"""SI-252 tests: dataset hierarchy label_action non-string tool safety.

SI-247 review note N1 (filed as SI-252): ``hierarchy.label_action``
called ``tool.startswith(...)`` with no type check, so a non-string
``tool`` raised a raw ``AttributeError`` -- bytes raised ``TypeError``
from the same call. The SI-247 builder had left it untouched on the
SI-239 section-5 rationale that tool names originate from the
server's own registry, never caller data. That premise does not
hold for the stored-row path: the public ``enrich_export_row`` feeds
``label_action`` the ``tool`` field straight from a stored export
row, so a row carrying ``{"tool": 123}`` crashed the backfill --
the same caller-shaped data class SI-247 fixed for
``intent_text`` / ``intent_level``.

The fix normalises in the consuming function, per the SI-247
precedent (``label_action`` also has a direct in-repo caller,
``recorder.SessionRecorder.record_action``, and is a public export,
so an enrich-only guard would leave the crash one layer down):

* ``label_action`` passes ``tool`` through the existing
  ``_normalised_text`` helper: a non-string tool is treated as
  absent (``""``).
* An absent tool produces exactly the result an empty string
  already produced at base: the unknown-action fallback
  ``{"op_level": 1, "op_label": ""}`` -- no ``human_`` prefix, no
  ``_TOOL_LEVELS`` hit, no ``add_notes_to_clip`` special case.
* ``enrich_export_row`` is unchanged: its guard still keys on the
  raw stored value (a truthy-but-unusable tool still triggers
  labelling, a falsy tool still does not, an ``audition`` row is
  still skipped) and the stored ``tool`` field is never rewritten;
  only the derived ``action_op_*`` fields reflect the normalised
  inference.

A new test file is used rather than extending the SI-247 file:
that file's header and pins are specifically the level/text
normalisation, and a separate file keeps this row's mutation
split (pins vs controls) independently countable.

Controls (valid string tools, the empty-string fallback, falsy
stored tools, audition rows) are byte-identical to base and marked
as controls; every other pin here failed or crashed at base.
"""

import pytest

from MCP_Server.dataset.hierarchy import enrich_export_row, label_action


# -- direct calls: crash shapes now degrade to the absent-tool result -------

@pytest.mark.parametrize(
    "bad",
    [123, 3.5, True, ["set_tempo"], {"x": 1}, {"set_tempo"}, ("set_tempo",), b"set_tempo", None],
)
def test_label_action_nonstring_tools_degrade_to_absent(bad):
    assert label_action(bad) == {"op_level": 1, "op_label": ""}


def test_label_action_bytes_human_tool_does_not_take_live_ui_branch():
    # Bytes startswith raises TypeError at base; at head the value is
    # absent, so it must NOT be recognised as a human_ live-UI action
    # (no "source": "live_ui" and the label is not the bytes value).
    assert label_action(b"human_click") == {"op_level": 1, "op_label": ""}


def test_label_action_nonstring_tool_ignores_add_notes_params():
    # The normalised absent tool is not "add_notes_to_clip", so a
    # phrase-sized notes_count in params cannot relabel it.
    assert label_action(123, {"notes_count": 10}) == {"op_level": 1, "op_label": ""}


# -- direct calls: controls, byte-identical to base --------------------------

def test_label_action_empty_string_control_unchanged():
    # Control: the empty string already produced this exact result at
    # base; it is the degrade target for the non-string pins above.
    assert label_action("") == {"op_level": 1, "op_label": ""}


@pytest.mark.parametrize(
    "tool,level,label",
    [
        ("set_tempo", 1, "adjust_tempo"),
        ("set_device_parameter", 1, "adjust_parameter"),
        ("create_audio_track", 2, "create_track"),
        ("create_clip", 2, "create_clip"),
        ("load_drum_kit", 2, "load_device"),
        ("duplicate_to_arrangement", 3, "arrange_section"),
    ],
)
def test_label_action_known_string_tools_control_unchanged(tool, level, label):
    assert label_action(tool) == {"op_level": level, "op_label": label}


def test_label_action_human_string_tool_control_unchanged():
    assert label_action("human_click") == {
        "op_level": 1,
        "op_label": "human_click",
        "source": "live_ui",
    }


def test_label_action_unknown_string_tool_control_unchanged():
    assert label_action("some_unknown_tool") == {
        "op_level": 1,
        "op_label": "some_unknown_tool",
    }


@pytest.mark.parametrize(
    "params,label",
    [
        ({"notes_count": 10}, "write_midi_phrase"),
        ({"notes_count": 2}, "edit_midi"),
        ({"notes": [0] * 8}, "write_midi_phrase"),
        ({"notes": [0] * 2}, "edit_midi"),
        (None, "edit_midi"),
    ],
)
def test_label_action_add_notes_string_tool_controls_unchanged(params, label):
    assert label_action("add_notes_to_clip", params) == {
        "op_level": 2,
        "op_label": label,
    }


# -- enrich_export_row: the stored-row surface -------------------------------

@pytest.mark.parametrize(
    "bad",
    [123, 3.5, True, ["set_tempo"], {"x": 1}, {"set_tempo"}, ("set_tempo",), b"set_tempo"],
)
def test_enrich_nonstring_tool_rows_no_longer_crash(bad):
    out = enrich_export_row({"tool": bad})
    assert out["action_op_level"] == 1
    assert out["action_op_label"] == ""
    # Design pin: the stored field is never rewritten; only the
    # derived action_op_* fields reflect the normalised inference.
    assert out["tool"] == bad
    assert type(out["tool"]) is type(bad)


def test_enrich_bytes_human_tool_row_degrades_to_absent():
    out = enrich_export_row({"tool": b"human_click"})
    assert out["action_op_level"] == 1
    assert out["action_op_label"] == ""
    assert out["tool"] == b"human_click"


def test_enrich_nonstring_tool_row_ignores_add_notes_params():
    out = enrich_export_row({"tool": 123, "params": {"notes_count": 10}})
    assert out["action_op_level"] == 1
    assert out["action_op_label"] == ""


# -- enrich_export_row: controls, byte-identical to base ---------------------

def test_enrich_string_tool_controls_unchanged():
    out = enrich_export_row({"tool": "set_tempo"})
    assert out["action_op_level"] == 1
    assert out["action_op_label"] == "adjust_tempo"
    out = enrich_export_row({"tool": "some_unknown_tool"})
    assert out["action_op_level"] == 1
    assert out["action_op_label"] == "some_unknown_tool"
    out = enrich_export_row({"tool": "human_click"})
    assert out["action_op_level"] == 1
    assert out["action_op_label"] == "human_click"
    # Control: a non-dict params value is already guarded at base.
    out = enrich_export_row({"tool": "set_tempo", "params": 123})
    assert out["action_op_label"] == "adjust_tempo"


@pytest.mark.parametrize("tool", ["", None, 0, False, [], {}])
def test_enrich_falsy_tool_controls_unchanged(tool):
    # Control: a falsy tool never triggered labelling at base,
    # whatever its type, and the row passes through untouched.
    out = enrich_export_row({"tool": tool})
    assert "action_op_level" not in out
    assert "action_op_label" not in out
    assert out["tool"] == tool


def test_enrich_audition_tool_controls_unchanged():
    # Control: audition rows skip action labelling at base for string
    # and non-string tools alike (the guard never calls label_action).
    assert enrich_export_row({"tool": "set_tempo", "row_type": "audition"}) == {
        "tool": "set_tempo",
        "row_type": "audition",
    }
    assert enrich_export_row({"tool": 123, "row_type": "audition"}) == {
        "tool": 123,
        "row_type": "audition",
    }
