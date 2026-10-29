"""MCP-15: the Ableton <-> Suno bridge operations.

This module holds the bridge logic with both edges injected:

* a :class:`~MCP_Server.suno_client.SunoClient` (faked in tests with
  a replay of the real tool schemas / response shapes), and
* a *placer* - the Live edge, with three methods:
  ``place_audio_file(path, track_name, tempo_bpm)`` returning the
  placement record, ``get_clip_slot(track_index, clip_index)``
  returning the slot's clip info (or None), and
  ``get_clip_file_path(track_index, clip_index)`` returning the file
  path Live itself reports for the slot's clip (or None when Live
  reports none). The real placer lives in ``MCP_Server.server`` and
  drives the existing Live connection; the test placer records
  calls.

Guardrails, as code (design of record:
hidden_files/ableton-suno-bridge-design-2026-10-29.md):

* **usage before generation** - a generation is refused when the
  account's remaining credits cannot cover the requested clip count;
* **clip_count pinned** - Suno defaults a sign-in caller through MCP
  to 2 clips per create; the bridge always sends an explicit count
  (1 unless the caller explicitly asked for 2);
* **duration_strict is always explicit** when a duration is given,
  never silently inherited;
* **idempotency** - every creation carries a key derived from the
  logical request, so a retry of the same request replays Suno's
  original instead of double-spending;
* **download credit gate** - downloads are first requested WITHOUT
  ``confirm``; a ``requires_credit`` answer stops the operation and
  is returned to the caller as a state. The bridge confirms a credit
  spend only when the caller passed ``allow_download_credit=True``;
* **bounded waits** - the wait-loop runs inside a total budget and
  reports ``pending`` honestly when it is exhausted, never claiming
  completion it did not observe;
* **analysis is best-effort** - a missing/failed timing map never
  blocks a placement; tempo is set in Live only when the caller
  asked for a tempo match AND a bpm was actually measured.
* **source-file binding (cover/extend)** - before any upload, the
  caller-supplied ``file_path`` is compared with the path Live
  itself reports for the addressed slot (via the placer's
  ``get_clip_file_path``, which reads the existing
  ``get_session_snapshot`` surface). A positive mismatch refuses
  the operation before any upload or credit spend; a match is
  carried in the result as ``source_file_verified: true``; when
  Live reports no path, the operation proceeds on the has-clip
  check alone and the result carries
  ``source_file_verified: false`` so the unchecked binding is
  visible, never silent.
"""

import hashlib
import json
import os
import re
import time
from pathlib import Path

from .suno_client import SunoError, SunoToolError

DEFAULT_WAIT_BUDGET_SECONDS = 600
_ANALYSIS_BUDGET_SECONDS = 120
_DOWNLOAD_POLL_SECONDS = 55
_CONTENT_TYPES = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".aif": "audio/aiff",
    ".aiff": "audio/aiff",
}


class SunoBridgeError(Exception):
    """A bridge-level refusal or failure (guardrail, bad input)."""


class InsufficientCreditsError(SunoBridgeError):
    """The account's remaining credits cannot cover the request."""


def derive_idempotency_key(kind, payload):
    """One key per logical creation: stable across retries of the
    same request, distinct across different requests."""
    canonical = json.dumps(payload, sort_keys=True, default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return "mcp15-%s-%s" % (kind, digest[:32])


def extract_clip_ids(payload):
    """The clip ids a creation answer names, in order."""
    if not isinstance(payload, dict):
        return []
    ids = []
    raw = payload.get("clip_ids")
    if isinstance(raw, list):
        ids.extend(x for x in raw if isinstance(x, str))
    for key in ("clip_id", "id"):
        value = payload.get(key)
        if isinstance(value, str) and value not in ids:
            ids.append(value)
    clips = payload.get("clips")
    if isinstance(clips, list):
        for clip in clips:
            if isinstance(clip, dict):
                cid = clip.get("id") or clip.get("clip_id")
                if isinstance(cid, str) and cid not in ids:
                    ids.append(cid)
    return ids


def _credits_remaining(usage):
    """credits_remaining from the verified account-usage shape:
    ``{"suno_account": {"credits_remaining": N, ...}, ...}``."""
    if not isinstance(usage, dict):
        return None
    account = usage.get("suno_account")
    if isinstance(account, dict) and isinstance(
        account.get("credits_remaining"), (int, float)
    ):
        return account["credits_remaining"]
    value = usage.get("credits_remaining")
    return value if isinstance(value, (int, float)) else None


def _is_requires_credit(payload=None, error=None):
    texts = []
    if isinstance(payload, dict):
        texts.extend(
            str(payload.get(k, ""))
            for k in ("status", "error", "code", "reason")
        )
        if payload.get("requires_credit") is True:
            return True
    if error is not None:
        texts.extend([str(error), str(getattr(error, "code", "") or "")])
    return any("requires_credit" in t for t in texts)


def _normalize_path(path):
    """One normal form for both sides of the source-file binding.

    ``~`` is expanded and the result is made absolute with every
    symlink, ``.`` and ``..`` component resolved (``os.path.realpath``
    does all three, lexically for any tail that does not exist).
    Case is deliberately NOT folded here: whether two differently
    cased spellings name the same file is a property of the
    filesystem, so that equivalence is settled by ``os.path.samefile``
    in :func:`_paths_match` instead of by guessing here.
    """
    return os.path.realpath(os.path.expanduser(str(path)))


def _paths_match(supplied, reported):
    """True when two path spellings name the same file.

    Equal after :func:`_normalize_path` (covers ``~``, symlinks and
    dot components), or the filesystem itself says so via
    ``os.path.samefile`` when both files exist (covers
    case-insensitive volumes such as the Mac's default APFS, where
    the spelling Live reports may differ in case from the caller's).
    """
    if _normalize_path(supplied) == _normalize_path(reported):
        return True
    try:
        return os.path.samefile(
            _normalize_path(supplied), _normalize_path(reported)
        )
    except (OSError, ValueError):
        return False


class SunoBridge:
    def __init__(self, client, placer, download_dir, *,
                 clock=time.monotonic, sleeper=time.sleep):
        self.client = client
        self.placer = placer
        self.download_dir = Path(download_dir)
        self._clock = clock
        self._sleeper = sleeper

    # ------------------------------------------------------------ reads

    def status(self):
        """Account + usage: the pre-generation guard surface."""
        account = self.client.account_me()
        usage = self.client.account_usage()
        return {
            "account": account,
            "usage": usage,
            "credits_remaining": _credits_remaining(usage),
        }

    def _guard_credits(self, needed):
        usage = self.client.account_usage()
        remaining = _credits_remaining(usage)
        if remaining is not None and remaining < needed:
            raise InsufficientCreditsError(
                "Suno credits cannot cover this request: %s remaining, "
                "%s needed for %d clip(s)"
                % (remaining, needed, needed)
            )
        return usage

    # -------------------------------------------------------- generation

    def generate(self, *, description=None, lyrics=None, style=None,
                 title=None, instrumental=False, duration=None,
                 duration_strict=False, model=None, clip_count=1,
                 voice_id=None,
                 wait_budget_seconds=DEFAULT_WAIT_BUDGET_SECONDS):
        """Create + wait to completion. No Live, no download."""
        if clip_count not in (1, 2):
            raise SunoBridgeError(
                "clip_count must be 1 or 2 (got %r); the bridge pins "
                "it explicitly because Suno defaults an MCP sign-in "
                "caller to 2" % (clip_count,)
            )
        if description and (lyrics or style):
            raise SunoBridgeError(
                "Simple mode (description) cannot be mixed with "
                "Custom mode (lyrics/style) - Suno answers 400"
            )
        if not description and not lyrics and not instrumental:
            raise SunoBridgeError(
                "Nothing to generate: pass a description, or lyrics, "
                "or instrumental=True"
            )
        self._guard_credits(clip_count)
        params = {
            "clip_count": clip_count,
            "instrumental": bool(instrumental),
            "idempotency_key": None,  # filled below, once params settle
        }
        if description:
            params["description"] = description
        if lyrics:
            params["lyrics"] = lyrics
        if style:
            params["style"] = style
        if title:
            params["title"] = title
        if model:
            params["model"] = model
        if voice_id:
            params["voice_id"] = voice_id
        if duration is not None:
            params["duration"] = duration
            # Always explicit when a duration is given - the design
            # forbids silently inheriting Suno's default (True).
            params["duration_strict"] = bool(duration_strict)
        params["idempotency_key"] = derive_idempotency_key(
            "create", {k: v for k, v in params.items()
                       if k != "idempotency_key"}
        )
        created = self.client.audio_create(**params)
        clip_ids = extract_clip_ids(created)
        if not clip_ids:
            raise SunoBridgeError(
                "Suno's create answer named no clip id: %r" % (created,)
            )
        return self._wait_result(clip_ids, wait_budget_seconds,
                                 extra={"created": created})

    def _wait_result(self, clip_ids, wait_budget_seconds, extra=None):
        outcome = self._wait_for_complete(clip_ids, wait_budget_seconds)
        result = dict(extra or {})
        result.update({
            "status": outcome["status"],
            "clip_ids": list(clip_ids),
            "clips": outcome["clips"],
            "waited_seconds": outcome["waited_seconds"],
        })
        if outcome["status"] == "pending":
            result["note"] = (
                "Still generating after the wait budget; the clip "
                "ids above are live - poll audio-retrieve or call "
                "again to keep waiting. Nothing was re-submitted."
            )
        return result

    def _wait_for_complete(self, clip_ids, wait_budget_seconds):
        """Loop audio-wait (each call blocks ~50 s server-side) inside
        a total budget; then take a final live read per clip."""
        started = self._clock()
        last = {}
        while True:
            last = self.client.audio_wait(
                clip_ids=list(clip_ids), until="complete"
            )
            status = _overall_status(last, clip_ids)
            if status in ("complete", "error"):
                break
            if self._clock() - started >= wait_budget_seconds:
                break
        clips = []
        final_status = "complete"
        for clip_id in clip_ids:
            try:
                clip = self.client.audio_retrieve(clip_id)
            except SunoError:
                clip = {}
                if isinstance(last, dict):
                    for candidate in last.get("clips") or []:
                        if isinstance(candidate, dict) and (
                            candidate.get("id") == clip_id
                        ):
                            clip = candidate
                            break
                    if not clip and last.get("id") == clip_id:
                        clip = last
            clips.append(clip)
            clip_status = clip.get("status")
            if clip_status == "error":
                final_status = "error"
            elif clip_status != "complete" and final_status == "complete":
                final_status = "pending"
        return {
            "status": final_status,
            "clips": clips,
            "waited_seconds": self._clock() - started,
        }

    # ---------------------------------------------------------- analysis

    def analysis_best_effort(self, clip_id,
                             budget_seconds=_ANALYSIS_BUDGET_SECONDS):
        """The timing map (bpm / key / beats / sections), or None.

        Analysis is a separate async model run: the first call starts
        it, parts arrive independently, and a clip can also simply
        have no analysis (one verified library clip answered
        not_found). Failure here never blocks a placement.
        """
        started = self._clock()
        try:
            while True:
                payload = self.client.audio_analysis(clip_id)
                status = payload.get("status")
                if status != "processing":
                    return payload if status == "complete" else payload or None
                if self._clock() - started >= budget_seconds:
                    return payload
                self._sleeper(2)
        except SunoError:
            return None

    # ---------------------------------------------------------- download

    def download(self, clip_id, *, format="wav",
                 allow_download_credit=False, title=None):
        """Download one clip's file into the download directory.

        Two-step credit rule: the first request never confirms. A
        requires_credit answer is returned as a state unless the
        caller explicitly allowed the spend.
        """
        payload = self._download_request(clip_id, format, confirm=False)
        if _is_requires_credit(payload):
            if not allow_download_credit:
                return {
                    "status": "requires_download_credit",
                    "clip_id": clip_id,
                    "format": format,
                    "note": (
                        "This download spends one download credit. "
                        "Re-call with allow_download_credit=True only "
                        "with the user's approval."
                    ),
                }
            payload = self._download_request(clip_id, format, confirm=True)
            if _is_requires_credit(payload):
                return {
                    "status": "requires_download_credit",
                    "clip_id": clip_id,
                    "format": format,
                    "note": "Suno still requires a credit after confirm.",
                }
        url = payload.get("url")
        if not url:
            return {
                "status": "no_url",
                "clip_id": clip_id,
                "format": format,
                "answer": payload,
            }
        self.download_dir.mkdir(parents=True, exist_ok=True)
        stem = _safe_stem(title or clip_id)
        dest = self.download_dir / ("%s.%s" % (stem, format))
        self.client.download_to_file(url, dest)
        return {
            "status": "downloaded",
            "clip_id": clip_id,
            "format": format,
            "path": str(dest),
            "url": url,
        }

    def _download_request(self, clip_id, format, confirm):
        started = self._clock()
        while True:
            try:
                payload = self.client.audio_download(
                    clip_id, format=format, confirm=confirm
                )
            except SunoToolError as error:
                if _is_requires_credit(error=error):
                    return {"status": "requires_credit",
                            "error": str(error),
                            "code": error.code}
                raise
            if isinstance(payload, dict) and (
                payload.get("status") == "processing"
                or payload.get("processing") is True
            ):
                # The download tool waits out processing itself for
                # ~50 s; if it still answers processing, the design
                # says call audio-download again - never audio-wait.
                if self._clock() - started >= _DOWNLOAD_POLL_SECONDS * 4:
                    return payload
                self._sleeper(1)
                continue
            return payload

    # --------------------------------------------------------- placement

    def _finish_to_live(self, clip_id, *, title, source, match_tempo,
                        allow_download_credit, extra_sidecar=None):
        """analysis (best-effort) -> download (gated) -> place into
        Live (verified by the placer) -> sidecar mapping file."""
        clip = {}
        try:
            clip = self.client.audio_retrieve(clip_id)
        except SunoError:
            clip = {}
        if not title:
            title = clip.get("title") or "Suno clip"
        analysis = self.analysis_best_effort(clip_id)
        downloaded = self.download(
            clip_id, allow_download_credit=allow_download_credit,
            title=title,
        )
        if downloaded["status"] != "downloaded":
            return {
                "status": downloaded["status"],
                "clip_id": clip_id,
                "download": downloaded,
                "analysis": _analysis_summary(analysis),
            }
        bpm = analysis.get("bpm") if isinstance(analysis, dict) else None
        placement = self.placer.place_audio_file(
            downloaded["path"],
            "%s [suno:%s]" % (title, clip_id),
            tempo_bpm=bpm if (match_tempo and bpm) else None,
        )
        sidecar = {
            "bridge": "mcp15",
            "source": source,
            "suno_clip_id": clip_id,
            "title": title,
            "song_url": clip.get("song_url"),
            "model": (clip.get("metadata") or {}).get("model")
                     if isinstance(clip.get("metadata"), dict) else None,
            "local_file": downloaded["path"],
            "track_index": placement.get("track_index"),
            "clip_index": placement.get("clip_index"),
            "bpm": bpm,
            "key": analysis.get("key") if isinstance(analysis, dict) else None,
            "tempo_matched": bool(match_tempo and bpm),
        }
        if extra_sidecar:
            sidecar.update(extra_sidecar)
        sidecar_path = Path(downloaded["path"]).with_suffix(".suno.json")
        sidecar_path.write_text(
            json.dumps(sidecar, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return {
            "status": "placed",
            "clip_id": clip_id,
            "title": title,
            "download": downloaded,
            "analysis": _analysis_summary(analysis),
            "placement": placement,
            "sidecar": str(sidecar_path),
            "sidecar_data": sidecar,
        }

    def generate_to_live(self, *, match_tempo=False,
                         allow_download_credit=False, **generate_kwargs):
        generated = self.generate(**generate_kwargs)
        if generated["status"] != "complete":
            return generated
        clip_id = generated["clip_ids"][0]
        prompt_fields = {
            k: generate_kwargs.get(k)
            for k in ("description", "lyrics", "style", "title", "model")
            if generate_kwargs.get(k)
        }
        placed = self._finish_to_live(
            clip_id,
            title=generate_kwargs.get("title"),
            source="generate",
            match_tempo=match_tempo,
            allow_download_credit=allow_download_credit,
            extra_sidecar={"prompt": prompt_fields},
        )
        placed["generated"] = {
            "clip_ids": generated["clip_ids"],
            "clips": generated["clips"],
        }
        return placed

    def library_to_live(self, *, clip_id=None, query=None,
                        match_tempo=False, allow_download_credit=False):
        """Place an existing library song - no generation cost."""
        if clip_id is None:
            if query:
                listing = self.client.library_search(query, limit=10)
            else:
                listing = self.client.library_list(limit=10)
            items = _library_items(listing)
            if not items:
                raise SunoBridgeError(
                    "No finished Suno library song matched %r"
                    % (query or "the library listing",)
                )
            clip_id = items[0].get("id") or items[0].get("clip_id")
            if not clip_id:
                raise SunoBridgeError(
                    "The library answer named no clip id: %r" % (items[0],)
                )
        clip = self.client.audio_retrieve(clip_id)
        if clip.get("status") not in (None, "complete", "streaming"):
            raise SunoBridgeError(
                "Library clip %s is not finished (status %r)"
                % (clip_id, clip.get("status"))
            )
        return self._finish_to_live(
            clip_id,
            title=clip.get("title"),
            source="library",
            match_tempo=match_tempo,
            allow_download_credit=allow_download_credit,
        )

    # ------------------------------------------------------------ upload

    def upload_file(self, file_path, *, title=None,
                    wait_budget_seconds=DEFAULT_WAIT_BUDGET_SECONDS):
        """The PUT flow: upload-create -> PUT bytes -> upload-complete
        -> poll to complete. Returns the uploaded clip's record."""
        path = Path(file_path)
        if not path.is_file():
            raise SunoBridgeError("No such audio file: %s" % (file_path,))
        data = path.read_bytes()
        session = self.client.audio_upload_create(
            title=title or path.stem
        )
        session_id = session.get("id") or session.get("clip_id")
        upload_url = session.get("upload_url")
        if not session_id or not upload_url:
            raise SunoBridgeError(
                "Suno's upload session named no id/upload_url: %r"
                % (session,)
            )
        content_type = _CONTENT_TYPES.get(
            path.suffix.lower(), "application/octet-stream"
        )
        self.client.put_bytes(upload_url, data, content_type)
        completed = self.client.audio_upload_complete(session_id)
        clip_id = (
            completed.get("id")
            or completed.get("clip_id")
            or session_id
        )
        # Uploads settle by polling audio-retrieve (the design's
        # flow), not audio-wait, which is a creation-side tool.
        clip = self._poll_retrieve(clip_id, wait_budget_seconds)
        if clip.get("status") != "complete":
            return {
                "status": clip.get("status") or "pending",
                "clip_id": clip_id,
                "note": "Upload did not reach complete in the budget.",
            }
        return clip

    def _poll_retrieve(self, clip_id, wait_budget_seconds):
        started = self._clock()
        clip = {}
        while True:
            clip = self.client.audio_retrieve(clip_id)
            if clip.get("status") in ("complete", "error"):
                return clip
            if self._clock() - started >= wait_budget_seconds:
                return clip
            self._sleeper(2)

    # ------------------------------------------------- cover / extend

    def _clip_slot_or_raise(self, track_index, clip_index):
        slot = self.placer.get_clip_slot(track_index, clip_index)
        if not slot or not slot.get("has_clip"):
            raise SunoBridgeError(
                "No clip at track %s, slot %s - nothing to cover/extend"
                % (track_index, clip_index)
            )
        return slot

    def _verify_source_file(self, track_index, clip_index, file_path):
        """Bind the caller-supplied file to the addressed Live clip.

        Asks the placer for the file path Live itself reports for
        the slot (the real placer reads the existing
        ``get_session_snapshot`` surface, whose session-clip
        serializer includes ``file_path`` when the LOM provides
        one) and compares it with ``file_path``:

        * match -> the binding record carries
          ``source_file_verified: True``;
        * positive mismatch (Live names a different absolute file)
          -> refuse here, before any upload or credit spend, naming
          both paths;
        * Live reports no path (or only a relative one, which this
          process cannot anchor to Live's own view of the disk) ->
          proceed on the has-clip check, with the record carrying
          ``source_file_verified: False`` so the unchecked binding
          is visible in the result rather than silent.

        Both sides are normalised the same way before comparison
        (:func:`_normalize_path`), and the filesystem arbitrates
        case and aliasing via :func:`_paths_match`. The record's
        ``file_path`` is the normalised caller path, which is what
        the upload then reads - so ``~`` and symlink spellings the
        caller used work end to end.
        """
        getter = getattr(self.placer, "get_clip_file_path", None)
        reported = getter(track_index, clip_index) if getter else None
        record = {
            "source_file_verified": False,
            "file_path": _normalize_path(file_path),
            "live_reported_file_path": reported or None,
        }
        if not reported:
            return record
        if _paths_match(file_path, reported):
            record["source_file_verified"] = True
            return record
        if os.path.isabs(os.path.expanduser(str(reported))):
            raise SunoBridgeError(
                "Refusing to upload: file_path %s does not match the "
                "file Live reports for track %s, slot %s (%s). "
                "Nothing was uploaded and no credits were spent."
                % (file_path, track_index, clip_index, reported)
            )
        return record

    def cover_clip(self, *, track_index, clip_index, file_path,
                   style=None, lyrics=None, keep_vocals=False,
                   voice_id=None, title=None, clip_count=1,
                   match_tempo=False, allow_download_credit=False,
                   wait_budget_seconds=DEFAULT_WAIT_BUDGET_SECONDS):
        slot = self._clip_slot_or_raise(track_index, clip_index)
        binding = self._verify_source_file(
            track_index, clip_index, file_path
        )
        uploaded = self.upload_file(binding["file_path"])
        if uploaded.get("status") != "complete":
            return {"status": uploaded.get("status", "pending"),
                    "uploaded": uploaded, **binding}
        source_id = uploaded.get("id") or uploaded.get("clip_id")
        # Cover lyrics rule (design): Suno does NOT carry the
        # source's lyrics over - omitted lyrics make the cover
        # instrumental unless a voice is set. When vocals should
        # survive and the caller gave no new lyrics, pass the source
        # clip's own lyrics explicitly.
        use_lyrics = lyrics
        if use_lyrics is None and keep_vocals:
            metadata = uploaded.get("metadata")
            if isinstance(metadata, dict):
                use_lyrics = metadata.get("lyrics") or None
        self._guard_credits(clip_count)
        params = {
            "clip_count": clip_count,
            "style": style,
            "lyrics": use_lyrics,
            "voice_id": voice_id,
            "title": title,
        }
        params["idempotency_key"] = derive_idempotency_key(
            "cover", {"source": source_id, **params}
        )
        created = self.client.audio_covers_create(source_id, **params)
        result = self._place_created(
            created, wait_budget_seconds,
            source="cover", match_tempo=match_tempo,
            allow_download_credit=allow_download_credit,
            title=title,
        )
        result.update(binding)
        return result

    def extend_clip(self, *, track_index, clip_index, file_path,
                    continue_at, lyrics=None, style=None,
                    instrumental=None, title=None, clip_count=1,
                    join_full_song=True, match_tempo=False,
                    allow_download_credit=False,
                    wait_budget_seconds=DEFAULT_WAIT_BUDGET_SECONDS):
        slot = self._clip_slot_or_raise(track_index, clip_index)
        binding = self._verify_source_file(
            track_index, clip_index, file_path
        )
        uploaded = self.upload_file(binding["file_path"])
        if uploaded.get("status") != "complete":
            return {"status": uploaded.get("status", "pending"),
                    "uploaded": uploaded, **binding}
        source_id = uploaded.get("id") or uploaded.get("clip_id")
        self._guard_credits(clip_count)
        params = {
            "clip_count": clip_count,
            "lyrics": lyrics,
            "style": style,
            "title": title,
        }
        if instrumental is not None:
            params["instrumental"] = bool(instrumental)
        params["idempotency_key"] = derive_idempotency_key(
            "extend", {"source": source_id, "continue_at": continue_at,
                       **params}
        )
        created = self.client.audio_extensions_create(
            source_id, continue_at, **params
        )
        waited = self._wait_result(
            extract_clip_ids(created), wait_budget_seconds,
            extra={"created": created},
        )
        if waited["status"] != "complete":
            waited.update(binding)
            return waited
        clip_id = waited["clip_ids"][0]
        if join_full_song:
            # The extension clip is only the continuation; join the
            # chain (free, idempotent) and place the whole song.
            joined = self.client.audio_full_song(
                clip_id,
                idempotency_key=derive_idempotency_key(
                    "full-song", {"tail": clip_id}
                ),
            )
            joined_ids = extract_clip_ids(joined)
            if joined_ids:
                joined_wait = self._wait_result(
                    joined_ids, wait_budget_seconds,
                    extra={"created": joined},
                )
                if joined_wait["status"] != "complete":
                    joined_wait.update(binding)
                    return joined_wait
                clip_id = joined_ids[0]
        result = self._finish_to_live(
            clip_id, title=title, source="extend",
            match_tempo=match_tempo,
            allow_download_credit=allow_download_credit,
        )
        result.update(binding)
        return result

    def _place_created(self, created, wait_budget_seconds, *, source,
                       match_tempo, allow_download_credit, title):
        clip_ids = extract_clip_ids(created)
        if not clip_ids:
            raise SunoBridgeError(
                "Suno's creation answer named no clip id: %r" % (created,)
            )
        waited = self._wait_result(
            clip_ids, wait_budget_seconds, extra={"created": created}
        )
        if waited["status"] != "complete":
            return waited
        return self._finish_to_live(
            clip_ids[0], title=title, source=source,
            match_tempo=match_tempo,
            allow_download_credit=allow_download_credit,
        )


def _overall_status(wait_payload, clip_ids):
    if not isinstance(wait_payload, dict):
        return "pending"
    status = wait_payload.get("status")
    if status in ("complete", "error"):
        return status
    clips = wait_payload.get("clips")
    if isinstance(clips, list) and clips:
        statuses = [
            c.get("status") for c in clips if isinstance(c, dict)
        ]
        if statuses and all(s == "complete" for s in statuses):
            return "complete"
        if any(s == "error" for s in statuses):
            return "error"
    return "pending"


def _analysis_summary(analysis):
    if not isinstance(analysis, dict):
        return None
    return {
        "status": analysis.get("status"),
        "bpm": analysis.get("bpm"),
        "key": analysis.get("key"),
        "time_signature": analysis.get("time_signature"),
        "sections": analysis.get("sections"),
    }


def _library_items(listing):
    if isinstance(listing, list):
        return listing
    if isinstance(listing, dict):
        for key in ("items", "clips", "songs", "results"):
            value = listing.get(key)
            if isinstance(value, list):
                return value
    return []


def _safe_stem(name):
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", str(name)).strip("._")
    return stem or "suno-clip"
