"""What the consent notice promises must actually hold.

consent.CONSENT_NOTICE tells the user "Email addresses and file paths are
stripped first". These tests pin that promise to the three places data leaves
the machine: session snapshots, intent text, and recorded action params.
"""
import json

import pytest

from MCP_Server.dataset import snapshot as snapshot_mod
from MCP_Server.dataset.recorder import scrub_text
from MCP_Server.dataset.trajectory_decorator import _extract_params

WINDOWS_PATH = r"C:\Users\ariadne\Music\Ableton\User Library\Samples\kick.wav"
POSIX_PATH = "/Users/ariadne/Music/Samples/kick.wav"
UNC_PATH = r"\\studio-nas\shared\ariadne\stems\vocal take 3.aif"


def _leaks(payload, needle="ariadne"):
    """True if the fake username survived anywhere in the payload."""
    return needle in json.dumps(payload, default=str).lower()


# ── Snapshots ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", [WINDOWS_PATH, POSIX_PATH, UNC_PATH])
def test_snapshot_strips_clip_file_paths(path):
    """Every audio clip carries file_path; it embeds the OS username."""
    raw = {"tracks": [{"clip_slots": [{"clip": {"name": "kick", "file_path": path}}]}]}
    cleaned = snapshot_mod.scrub_snapshot(raw)
    assert not _leaks(cleaned)
    assert cleaned["tracks"][0]["clip_slots"][0]["clip"]["file_path"].startswith("<path:")


def test_snapshot_path_keeps_shape_not_content():
    cleaned = snapshot_mod.scrub_snapshot({"file_path": POSIX_PATH})
    # 5 segments (Users/ariadne/Music/Samples/kick.wav), .wav extension
    assert cleaned["file_path"] == "<path:5.wav>"


@pytest.mark.parametrize("filename, expected", [
    ("kick.wav", "<path:1.wav>"),
    ("KICK.AIFF", "<path:1.aiff>"),
    # Only a real extension survives; anything else is not one, so it is dropped
    # rather than carried out with the placeholder.
    ("take.na!me", "<path:1>"),
    ("ariadne's mix.v2 final", "<path:1>"),
    (".hidden", "<path:1>"),          # no stem, so no extension
    ("no-extension", "<path:1>"),
    ("stem.reallylongextension", "<path:1>"),
])
def test_snapshot_path_extension_is_an_extension_or_nothing(filename, expected):
    cleaned = snapshot_mod.scrub_snapshot({"file_path": filename})
    assert cleaned["file_path"] == expected
    assert not _leaks(cleaned, "ariadne")
    assert "!" not in cleaned["file_path"]


def test_snapshot_redacts_user_authored_names_but_keeps_live_defaults():
    raw = {"tracks": [
        {"name": "Audio Track 3"},
        {"name": "Kick"},
        {"name": "chorus for ariadne's wedding"},
    ]}
    cleaned = snapshot_mod.scrub_snapshot(raw)
    assert cleaned["tracks"][0]["name"] == "Audio Track 3"
    assert cleaned["tracks"][1]["name"] == "Kick"
    assert cleaned["tracks"][2]["name"].startswith("<name:")
    assert not _leaks(cleaned)


def test_snapshot_scrubs_paths_at_any_depth():
    raw = {"a": [{"b": {"c": [{"path": UNC_PATH}]}}]}
    assert not _leaks(snapshot_mod.scrub_snapshot(raw))


def test_snapshot_leaves_non_string_and_numeric_state_alone():
    raw = {"tempo": 128.0, "notes": [[60, 0.0, 0.25, 100]], "mute": False, "file_path": None}
    assert snapshot_mod.scrub_snapshot(raw) == raw


# ── Free text ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "render to " + WINDOWS_PATH,
    "compare against " + POSIX_PATH,
    "send the stems to ariadne@example.com",
])
def test_scrub_text_removes_paths_and_emails(text):
    assert not _leaks(scrub_text(text))


def test_scrub_text_keeps_the_musical_content():
    cleaned = scrub_text("make the chorus hit harder, reference " + POSIX_PATH)
    assert "make the chorus hit harder" in cleaned


# ── Recorded action params ───────────────────────────────────────────────────

def test_extract_params_reduces_a_file_path_to_its_extension():
    params = _extract_params({"path": WINDOWS_PATH})
    assert not _leaks(params)
    assert params == {"file_extension": ".wav", "has_path": True}


def test_extract_params_keeps_browser_paths_which_name_only_lives_tree():
    params = _extract_params({"path": "Drums/Kits/808 Core Kit"})
    assert params["browser_path"] == "Drums/Kits/808 Core Kit"


@pytest.mark.parametrize("key", ["text", "note", "search_query"])
def test_extract_params_scrubs_free_text_fields(key):
    assert not _leaks(_extract_params({key: "like " + POSIX_PATH + " but warmer"}))


def test_extract_params_redacts_user_authored_names():
    assert not _leaks(_extract_params({"name": "ariadne demo v4"}))


def test_extract_params_keeps_indices_and_note_data():
    params = _extract_params({
        "track_index": 2,
        "clip_index": 0,
        "notes": [{"pitch": 60, "start_time": 0.0, "duration": 0.25, "velocity": 100}],
    })
    assert params["track_index"] == 2
    assert params["notes_count"] == 1
    assert params["notes"] == [[60, 0.0, 0.25, 100]]


# ── Intent text reaches the scrubber through both entry points ───────────────

@pytest.fixture
def recorder():
    """A real recorder. Rows only reach its in-memory queue: the worker checks
    dataset_enabled() before writing, and these tests never grant consent."""
    from MCP_Server.dataset.recorder import SessionRecorder

    return SessionRecorder(session_id="sess_test", customer_uuid="uuid_test")


def test_submit_intent_text_is_scrubbed(recorder):
    """submit_intent reaches set_intent directly, bypassing observe_prompt."""
    event = recorder.set_intent("bounce it next to " + WINDOWS_PATH, level=5)
    assert not _leaks(event.text)
    assert not _leaks(recorder._active_intent_text)


def test_observed_prompt_is_scrubbed(recorder):
    event = recorder.observe_prompt("open " + POSIX_PATH + " and warm it up")
    assert not _leaks(event.text)


def test_intent_of_nothing_but_a_path_records_only_its_placeholder(recorder):
    event = recorder.set_intent(POSIX_PATH)
    assert not _leaks(event.text)
    assert "<path>" in event.text


def test_empty_intent_is_refused(recorder):
    with pytest.raises(ValueError):
        recorder.set_intent("   ")


def test_recorded_action_error_is_scrubbed(recorder):
    event = recorder.record_action(
        tool="create_audio_clip",
        params={"track_index": 0},
        success=False,
        error="Error creating audio clip: no such file " + WINDOWS_PATH,
    )
    assert not _leaks(event.error)


# ── SI-248: non-string path/name must not bypass the scrub ──────────────────

def test_extract_params_reduces_a_bytes_file_path_to_metadata_only():
    params = _extract_params({"path": b"/Users/ariadne/Music/Samples/kick.wav"})
    assert params == {"file_extension": "unknown", "has_path": True}
    assert not _leaks(params)


def test_extract_params_reduces_a_bytearray_path_to_metadata_only():
    params = _extract_params({"path": bytearray(b"/Users/ariadne/secret.wav")})
    assert params == {"file_extension": "unknown", "has_path": True}
    assert not _leaks(params)


def test_extract_params_reduces_a_nonstring_path_value_to_metadata_only():
    params = _extract_params({"path": 123})
    assert params == {"file_extension": "unknown", "has_path": True}


def test_extract_params_reduces_a_bytes_name_to_a_length_placeholder():
    params = _extract_params({"name": b"ariadne demo v4"})
    assert params == {"name": "<name:15>"}
    assert not _leaks(params)


def test_extract_params_reduces_a_bytearray_name_to_a_length_placeholder():
    params = _extract_params({"name": bytearray(b"ariadne demo v4")})
    assert params == {"name": "<name:15>"}
    assert not _leaks(params)


def test_extract_params_reduces_a_container_name_to_a_length_placeholder():
    params = _extract_params({"name": ["ariadne demo"]})
    assert params == {"name": "<name:1>"}
    assert not _leaks(params)


def test_extract_params_reduces_a_scalar_name_to_a_length_placeholder():
    params = _extract_params({"name": 123})
    assert params == {"name": "<name:0>"}
# -- SI-250: scrub_snapshot depth bound and cycle guard ---------------------
#
# At base (c247c7e) every shape below raised RecursionError: a list or
# dict nested 2000 deep, a self-referential dict, a self-referential
# list, and a dict<->list cycle (SI-239 F11, probes D01-D03). Depth 200
# was fine at base; it now refuses by design, because the bound is set
# from the legitimate LOM shape (about 23 containers deep at the Remote
# Script rack-chain cap of 4), not from the recursion limit: 100 is
# more than 4x legitimate depth. The refusal voice is ValueError, the
# same voice stable_hash uses for a circular object, and
# fetch_snapshot degrades a refused scrub to its own failure voice:
# None.

def _nested_list(depth):
    root = []
    cur = root
    for _ in range(depth):
        nxt = []
        cur.append(nxt)
        cur = nxt
    return root


def _nested_dict(depth):
    root = {}
    cur = root
    for _ in range(depth):
        nxt = {}
        cur["a"] = nxt
        cur = nxt
    return root


def test_scrub_refuses_deeply_nested_list():
    with pytest.raises(ValueError, match="nested too deeply"):
        snapshot_mod.scrub_snapshot(_nested_list(2000))


def test_scrub_refuses_deeply_nested_dict():
    with pytest.raises(ValueError, match="nested too deeply"):
        snapshot_mod.scrub_snapshot(_nested_dict(2000))


def test_scrub_refuses_snapshot_just_over_the_bound():
    over = snapshot_mod._MAX_SCRUB_DEPTH + 10
    with pytest.raises(ValueError, match="nested too deeply"):
        snapshot_mod.scrub_snapshot(_nested_list(over))


def test_scrub_accepts_depth_well_above_any_real_snapshot():
    # 50 containers deep is more than twice the deepest legitimate
    # LOM snapshot (about 23) and must scrub byte-identically.
    raw = _nested_list(50)
    assert snapshot_mod.scrub_snapshot(raw) == raw
    raw = _nested_dict(50)
    assert snapshot_mod.scrub_snapshot(raw) == raw


def test_scrub_refuses_circular_dict():
    raw = {}
    raw["self"] = raw
    with pytest.raises(ValueError, match="Circular reference detected"):
        snapshot_mod.scrub_snapshot(raw)


def test_scrub_refuses_circular_list():
    raw = []
    raw.append(raw)
    with pytest.raises(ValueError, match="Circular reference detected"):
        snapshot_mod.scrub_snapshot(raw)


def test_scrub_refuses_mixed_dict_list_cycle():
    raw = {}
    inner = [raw]
    raw["inner"] = inner
    with pytest.raises(ValueError, match="Circular reference detected"):
        snapshot_mod.scrub_snapshot(raw)


def test_scrub_allows_shared_subobject_that_is_not_a_cycle():
    shared = {"name": "Kick", "file_path": POSIX_PATH}
    raw = {"a": shared, "b": [shared]}
    cleaned = snapshot_mod.scrub_snapshot(raw)
    assert cleaned["a"]["file_path"] == "<path:5.wav>"
    assert cleaned["b"][0]["file_path"] == "<path:5.wav>"
    assert not _leaks(cleaned)


def test_fetch_snapshot_returns_none_for_circular_snapshot():
    raw = {}
    raw["self"] = raw
    assert snapshot_mod.fetch_snapshot(lambda *a, **k: raw) is None


def test_fetch_snapshot_returns_none_for_deeply_nested_snapshot():
    raw = _nested_list(2000)
    assert snapshot_mod.fetch_snapshot(lambda *a, **k: raw) is None


def test_fetch_snapshot_still_returns_scrubbed_normal_snapshot():
    raw = {"tracks": [{"name": "chorus for ariadne", "file_path": POSIX_PATH}]}
    cleaned = snapshot_mod.fetch_snapshot(lambda *a, **k: raw)
    assert cleaned is not None
    assert not _leaks(cleaned)
