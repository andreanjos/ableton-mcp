"""Heuristic hierarchical labels (Level 1–5) for trajectory events."""

from __future__ import annotations

import re
from typing import Any

_TOOL_LEVELS: dict[str, tuple[int, str]] = {
    "set_tempo": (1, "adjust_tempo"),
    "set_device_parameter": (1, "adjust_parameter"),
    "set_device_parameters": (1, "adjust_parameter"),
    "set_track_name": (1, "rename_track"),
    "set_clip_name": (1, "rename_clip"),
    "set_arrangement_clip_name": (1, "rename_clip"),
    "fire_clip": (1, "trigger_clip"),
    "stop_clip": (1, "stop_clip"),
    "start_playback": (1, "transport"),
    "stop_playback": (1, "transport"),
    "set_arrangement_time": (1, "move_playhead"),
    "switch_to_arrangement_view": (1, "change_view"),
    "create_midi_track": (2, "create_track"),
    "create_audio_track": (2, "create_track"),
    "create_clip": (2, "create_clip"),
    "create_audio_clip": (2, "create_clip"),
    "add_notes_to_clip": (2, "edit_midi"),
    "load_instrument_or_effect": (2, "load_device"),
    "load_drum_kit": (2, "load_device"),
    "duplicate_to_arrangement": (3, "arrange_section"),
}

_SECTION_RE = re.compile(
    r"\b(intro|verse|chorus|drop|bridge|outro|build|breakdown|pre[- ]?chorus|hook)\b",
    re.I,
)
_SONG_RE = re.compile(
    r"\b(whole (song|track)|full (song|track)|finish (the )?(song|track|arrangement)|"
    r"complete (the )?(song|track)|change genre|restyle)\b",
    re.I,
)
_AESTHETIC_RE = re.compile(
    r"\b(darker|brighter|warmer|colder|nostalgic|energetic|aggressive|chill|"
    r"emotional|dreamy|gritty|clean|human|robotic|like .+)\b",
    re.I,
)
_MIX_RE = re.compile(
    r"\b(mix|muddy|loudness|sidechain|compress|eq|reverb|space|sit in the mix)\b",
    re.I,
)


_ATOMIC_RE = re.compile(
    r"\b(set|change|adjust|rename|nudge|turn (up|down)|increase|decrease)\b.{0,40}"
    r"\b(tempo|bpm|volume|pan|name|level|value|parameter|knob)\b",
    re.I,
)
_MUSICAL_OP_RE = re.compile(
    r"\b(add|write|create|make|load|duplicate|delete|remove|play|insert)\b.{0,40}"
    r"\b(clip|track|note|midi|melody|bassline|chord|drum|kit|beat|loop|instrument|"
    r"effect|preset|sample|pattern)\b",
    re.I,
)


def _normalised_level(value: Any) -> int | None:
    """Return *value* only when it is a real int level in [1, 5].

    Anything else -- bools (``type(True) is bool``, not ``int``), floats
    (including integral floats, NaN and +/-Infinity), strings, containers
    -- is treated as absent, exactly as the out-of-range ints 0 and 6
    have always been treated: the caller falls back to text inference.
    """
    if type(value) is int and 1 <= value <= 5:
        return value
    return None


def _normalised_text(value: Any) -> str:
    """Return *value* only when it is a string; anything else is absent.

    Stored/exported rows are data-shaped, so a non-string ``intent_text``
    (a number, a list, bytes, ...) must degrade to "no text" instead of
    raising the regex engine TypeError on the first pattern search.
    """
    return value if isinstance(value, str) else ""


def infer_intent_level(text: str, explicit_level: int | None = None) -> int:
    """Infer Level 1–5 from intent text (explicit wins if provided).

    Ordered most-specific first: an atomic tweak is more specific than a
    musical operation, which is more specific than a section/song goal.
    Aesthetic and mix language stays at 5 — those are open-ended goals.

    ``explicit_level`` is normalised by :func:`_normalised_level` and
    ``text`` by :func:`_normalised_text`: unusable values are treated
    as absent rather than compared or searched unchecked.
    """
    explicit = _normalised_level(explicit_level)
    if explicit is not None:
        return explicit
    text = _normalised_text(text)
    if not text:
        return 5
    if _SONG_RE.search(text):
        return 4
    if _SECTION_RE.search(text):
        return 3
    if _ATOMIC_RE.search(text):
        return 1
    if _MUSICAL_OP_RE.search(text):
        return 2
    return 5


def infer_intent_op_label(text: str, level: int) -> str:
    text = _normalised_text(text)
    if not text:
        return "creative_goal"
    m = _SECTION_RE.search(text)
    if m:
        section = m.group(1).lower().replace(" ", "_")
        if "bigger" in text.lower() or "larger" in text.lower():
            return f"make_{section}_bigger"
        if "smaller" in text.lower() or "quieter" in text.lower():
            return f"make_{section}_smaller"
        return f"edit_{section}"
    if _SONG_RE.search(text):
        return "song_level_change"
    if _AESTHETIC_RE.search(text):
        return "aesthetic_shift"
    if _MIX_RE.search(text):
        return "mix_improve"
    if level <= 2:
        return "musical_operation"
    return "creative_goal"


def label_action(tool: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return {op_level, op_label, category_hint} for an MCP tool action."""
    params = params or {}
    if tool.startswith("human_"):
        return {
            "op_level": 1,
            "op_label": tool,
            "source": "live_ui",
        }

    base = _TOOL_LEVELS.get(tool)
    if base:
        level, label = base
    else:
        level, label = 1, tool

    if tool == "add_notes_to_clip":
        count = params.get("notes_count")
        if count is None and isinstance(params.get("notes"), list):
            count = len(params["notes"])
        if isinstance(count, int) and count >= 8:
            label = "write_midi_phrase"

    return {"op_level": level, "op_label": label}


def label_intent(text: str, level: int | None = None) -> dict[str, Any]:
    resolved = infer_intent_level(text, level)
    return {
        "op_level": resolved,
        "op_label": infer_intent_op_label(text, resolved),
        "intent_level": resolved,
    }


def enrich_export_row(row: dict[str, Any]) -> dict[str, Any]:
    """Backfill hierarchy fields on an exported training row."""
    out = dict(row)
    raw_text = out.get("intent_text")
    # Text normalisation at this call site: a non-string intent_text is
    # treated as absent for labelling. The guard below still keys on the
    # raw values, so its truthiness is unchanged for every row (a present
    # but unusable level such as 6 -- or "3" -- still triggers labelling,
    # and a falsy text/level still does not). The stored fields in *out*
    # are never rewritten: an invalid stored intent_level stays as
    # stored, exactly as 0 and 6 already did, and only the derived
    # intent_op_* fields reflect the normalised inference.
    intent_text = raw_text if isinstance(raw_text, str) else ""
    intent_level = out.get("intent_level")
    if raw_text or intent_level:
        labels = label_intent(intent_text, intent_level)
        out.setdefault("intent_level", labels["intent_level"])
        out["intent_op_label"] = labels["op_label"]
        out["intent_op_level"] = labels["op_level"]

    tool = out.get("tool")
    if tool and out.get("row_type") != "audition":
        al = label_action(tool, out.get("params") if isinstance(out.get("params"), dict) else {})
        out["action_op_level"] = al["op_level"]
        out["action_op_label"] = al["op_label"]
    return out
