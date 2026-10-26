"""SI-248 — telemetry extractor type-safety.

F9 (SI-239): telemetry_decorator._extract_tool_params stored a non-string
``name`` verbatim, and with capture_notes=True it did arithmetic on the raw
pitch, so a string / NaN / list pitch raised inside the rich-telemetry
wrappers' swallow-all finally and silently dropped the whole telemetry
event. The trajectory-side pins for the same row live in
tests/test_dataset_scrubbing.py; this module had no existing test file.

Controls (string name/path, pitch -5 and 60) are byte-identical to base.
A pitch that cannot be named gets a "?" placeholder in its own readable
entry; the compact entry and every other event field survive.
"""
import math

import pytest

from MCP_Server.telemetry_decorator import _extract_tool_params


def _params_with_pitch(pitch):
    return _extract_tool_params(
        {"notes": [{"pitch": pitch, "start_time": 0, "duration": 1, "velocity": 100}]},
        capture_notes=True,
    )


# ── Non-string name / path ───────────────────────────────────────────────────

@pytest.mark.parametrize("value", [
    b"chorus for Sarah",
    bytearray(b"chorus for Sarah"),
    ["chorus for Sarah"],
    {"x": 1},
    123,
])
def test_nonstring_name_is_dropped_not_stored_verbatim(value):
    assert _extract_tool_params({"name": value}) == {}


def test_string_name_control_is_captured_verbatim_with_consent():
    assert _extract_tool_params({"name": "chorus for Sarah"}) == {"name": "chorus for Sarah"}


def test_bytes_path_is_dropped_not_stored_verbatim():
    # Already safe at base (the path branch is str-gated and stores nothing
    # for a non-string); pinned here so it cannot regress into raw storage.
    assert _extract_tool_params({"path": b"/Users/ariadne/secret.wav"}) == {}


def test_string_path_control_reduces_to_extension_only():
    assert _extract_tool_params({"path": "/Users/ariadne/secret.wav"}) == {
        "file_extension": ".wav",
        "has_path": True,
    }


# ── Bad pitches cannot kill the event ────────────────────────────────────────

@pytest.mark.parametrize("pitch", ["A", [60], {"x": 1}, None, float("inf"), float("-inf"), 60.0])
def test_unnamable_pitch_gets_a_placeholder_and_the_event_survives(pitch):
    params = _params_with_pitch(pitch)
    assert params["notes_count"] == 1
    assert params["notes"] == [[pitch, 0, 1, 100]]
    assert params["notes_readable"] == [["?", 0, 1, 100]]


def test_nan_pitch_gets_a_placeholder_and_the_event_survives():
    params = _params_with_pitch(float("nan"))
    assert params["notes_count"] == 1
    assert math.isnan(params["notes"][0][0])
    assert params["notes_readable"] == [["?", 0, 1, 100]]


def test_one_bad_note_cannot_kill_the_other_notes_or_the_event():
    params = _extract_tool_params(
        {
            "track_index": 2,
            "notes": [
                {"pitch": 60, "start_time": 0, "duration": 1, "velocity": 100},
                {"pitch": "A", "start_time": 1, "duration": 1, "velocity": 100},
            ],
        },
        capture_notes=True,
    )
    assert params["track_index"] == 2
    assert params["notes_count"] == 2
    assert params["notes"] == [[60, 0, 1, 100], ["A", 1, 1, 100]]
    assert params["notes_readable"] == [["C4", 0, 1, 100], ["?", 1, 1, 100]]


# ── Controls: valid pitches are byte-identical ───────────────────────────────

def test_negative_pitch_control_is_byte_identical():
    params = _params_with_pitch(-5)
    assert params["notes"] == [[-5, 0, 1, 100]]
    assert params["notes_readable"] == [["G-2", 0, 1, 100]]


def test_middle_c_control_is_byte_identical():
    params = _params_with_pitch(60)
    assert params["notes"] == [[60, 0, 1, 100]]
    assert params["notes_readable"] == [["C4", 0, 1, 100]]
