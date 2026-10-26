"""SI-247 tests: dataset hierarchy explicit-level / text type-safety.

SI-239 finding F8: ``hierarchy.infer_intent_level`` compared the
caller's ``explicit_level`` against ints with no type check -- a
string/list/dict level raised a raw TypeError, a float level (2.5, or
the integral 2.0) and ``True`` were returned as-is as the "level",
and NaN/+/-Infinity were silently ignored. The public
``enrich_export_row`` feeds it ``intent_level`` straight from a stored
row dict, so an export row whose level was serialised as a string
("3") crashed the backfill. In the same file, a non-string ``text``
raised the regex engine's TypeError in ``infer_intent_level`` /
``label_intent`` / ``infer_intent_op_label``; ``enrich_export_row``
guarded the row's ``intent_text`` only by truthiness, so a numeric
``intent_text`` took the same crash path.

The fix normalises at the consuming functions:

* ``_normalised_level`` accepts only a real int in [1, 5]
  (``type(value) is int``, so bools are excluded) and treats anything
  else as absent -- exactly the long-standing behaviour for the
  out-of-range ints 0 and 6, which fall back to text inference.
  "3" is NOT parsed to 3 and 2.0 is NOT truncated to 2.
* ``_normalised_text`` accepts only a string and treats anything else
  as absent (""), in ``infer_intent_level``,
  ``infer_intent_op_label`` and ``enrich_export_row``.
* ``enrich_export_row`` keeps its guard keyed on the raw row values
  (truthiness unchanged for every row: a present-but-unusable level
  such as 6 -- or "3" -- still triggers labelling, a falsy text/level
  still does not) and never rewrites the stored fields: an invalid
  stored ``intent_level`` stays as stored, exactly as 0 and 6 already
  did; only the derived ``intent_op_*`` fields reflect the normalised
  inference.

Controls (valid levels 1-5, 0/6 fallback, NaN/Infinity fallback,
falsy text, valid text) are byte-identical to base and marked as
controls; every other pin here failed or crashed at base.
"""
import math

import pytest

from MCP_Server.dataset.hierarchy import (
    enrich_export_row,
    infer_intent_level,
    infer_intent_op_label,
    label_intent,
)

CHORUS = "make a chorus"  # text inference -> level 3, label edit_chorus
SONG = "finish the song"  # text inference -> level 4


# -- explicit level: crash shapes now fall back to text inference -----------

@pytest.mark.parametrize("bad", ["3", [3], {"x": 1}, {3}, (3,), b"3"])
def test_infer_string_and_container_levels_fall_back_to_text(bad):
    result = infer_intent_level(CHORUS, bad)
    assert result == 3
    assert type(result) is int


@pytest.mark.parametrize("bad", [2.5, 2.0])
def test_infer_float_levels_are_not_returned_as_levels(bad):
    result = infer_intent_level(CHORUS, bad)
    assert result == 3
    assert type(result) is int


def test_infer_bool_level_is_not_returned_as_level():
    result = infer_intent_level(CHORUS, True)
    assert result == 3
    assert type(result) is int


def test_infer_string_level_with_empty_text_falls_back_to_default():
    assert infer_intent_level("", "3") == 5


def test_infer_float_level_falls_back_to_other_text_inference():
    assert infer_intent_level(SONG, 2.5) == 4


# -- explicit level: controls, byte-identical to base ------------------------

@pytest.mark.parametrize("level", [1, 2, 3, 4, 5])
def test_infer_valid_levels_win_unchanged(level):
    assert infer_intent_level(CHORUS, level) == level


@pytest.mark.parametrize("level", [0, 6, -1, None, False])
def test_infer_out_of_range_and_absent_levels_fall_back_unchanged(level):
    # Control: 0/6 already fell back to text inference by design.
    assert infer_intent_level(CHORUS, level) == 3


def test_infer_negative_float_level_falls_back_unchanged():
    # Control: -0.5 already fell back at base -- it fails the range
    # comparison, unlike the in-range floats 2.5/2.0 pinned above.
    assert infer_intent_level(CHORUS, -0.5) == 3


@pytest.mark.parametrize("level", [float("nan"), float("inf"), float("-inf")])
def test_infer_nonfinite_levels_fall_back_unchanged(level):
    # Control: NaN/+/-Inf already fell through both comparisons at base;
    # they are now absent by type instead of by comparison accident.
    assert infer_intent_level(CHORUS, level) == 3


# -- text: non-string shapes now treated as absent ---------------------------

@pytest.mark.parametrize("bad", [123, 3.5, ["make a chorus"], {"x": 1}, b"make a chorus", True])
def test_infer_nonstring_text_treated_as_absent(bad):
    assert infer_intent_level(bad, None) == 5


@pytest.mark.parametrize("bad", [123, 3.5, ["x"], {"x": 1}, b"x", True])
def test_label_intent_nonstring_text_treated_as_absent(bad):
    assert label_intent(bad, None) == {
        "op_level": 5,
        "op_label": "creative_goal",
        "intent_level": 5,
    }


@pytest.mark.parametrize("bad", [123, 3.5, ["x"], {"x": 1}, b"x", True])
def test_op_label_nonstring_text_treated_as_absent(bad):
    assert infer_intent_op_label(bad, 3) == "creative_goal"


def test_label_intent_nonstring_text_with_bad_level():
    assert label_intent(123, "3") == {
        "op_level": 5,
        "op_label": "creative_goal",
        "intent_level": 5,
    }


def test_label_intent_nonstring_text_with_valid_level_uses_level():
    # The valid explicit level wins; the label is computed on absent text.
    assert label_intent(123, 3) == {
        "op_level": 3,
        "op_label": "creative_goal",
        "intent_level": 3,
    }


# -- text / level controls, byte-identical to base ---------------------------

def test_text_controls_unchanged():
    assert infer_intent_level(CHORUS, None) == 3
    assert infer_intent_level("", None) == 5
    assert infer_intent_level(None, None) == 5
    assert infer_intent_level(0, None) == 5
    assert label_intent(CHORUS, None) == {
        "op_level": 3,
        "op_label": "edit_chorus",
        "intent_level": 3,
    }
    assert infer_intent_op_label(CHORUS, 3) == "edit_chorus"
    # Control: a valid explicit level already short-circuited before the
    # text was ever searched at base.
    assert infer_intent_level(123, 3) == 3


# -- enrich_export_row: the stored-row surface --------------------------------

def test_enrich_string_level_row_no_longer_crashes():
    out = enrich_export_row({"intent_text": CHORUS, "intent_level": "3"})
    assert out["intent_op_level"] == 3
    assert out["intent_op_label"] == "edit_chorus"
    # Design pin: the stored field is never rewritten (0/6 precedent);
    # only the derived op fields reflect the normalised inference.
    assert out["intent_level"] == "3"


@pytest.mark.parametrize("bad", [[3], {"x": 1}, {3}, (3,), b"3"])
def test_enrich_container_level_rows_fall_back_to_text(bad):
    out = enrich_export_row({"intent_text": CHORUS, "intent_level": bad})
    assert out["intent_op_level"] == 3
    assert out["intent_op_label"] == "edit_chorus"


def test_enrich_float_level_row_falls_back_to_text():
    out = enrich_export_row({"intent_text": CHORUS, "intent_level": 2.5})
    assert out["intent_op_level"] == 3
    assert type(out["intent_op_level"]) is int
    assert out["intent_level"] == 2.5


def test_enrich_bool_level_row_falls_back_to_text():
    out = enrich_export_row({"intent_text": CHORUS, "intent_level": True})
    assert out["intent_op_level"] == 3
    assert type(out["intent_op_level"]) is int
    assert out["intent_level"] is True


@pytest.mark.parametrize("level", [float("nan"), float("inf"), float("-inf")])
def test_enrich_nonfinite_level_rows_unchanged(level):
    # Control: these already produced the text-inferred op level at base.
    out = enrich_export_row({"intent_text": CHORUS, "intent_level": level})
    assert out["intent_op_level"] == 3
    assert out["intent_op_label"] == "edit_chorus"


def test_enrich_nan_level_stays_stored_as_nan():
    out = enrich_export_row({"intent_text": CHORUS, "intent_level": float("nan")})
    assert math.isnan(out["intent_level"])


@pytest.mark.parametrize("level,stored", [(0, 0), (6, 6)])
def test_enrich_out_of_range_level_controls_unchanged(level, stored):
    # Control: 0/6 already fell back to text inference at base and the
    # stored value was preserved by setdefault.
    out = enrich_export_row({"intent_text": CHORUS, "intent_level": level})
    assert out["intent_op_level"] == 3
    assert out["intent_level"] is stored or out["intent_level"] == stored


def test_enrich_string_level_without_text_still_labels():
    # A present-but-unusable level triggers labelling exactly as 6 does
    # (control below); inference on absent text is the default level 5.
    out = enrich_export_row({"intent_level": "3"})
    assert out["intent_op_level"] == 5
    assert out["intent_op_label"] == "creative_goal"
    assert out["intent_level"] == "3"


def test_enrich_out_of_range_level_without_text_control():
    out = enrich_export_row({"intent_level": 6})
    assert out["intent_op_level"] == 5
    assert out["intent_level"] == 6


def test_enrich_valid_level_and_text_controls_unchanged():
    out = enrich_export_row({"intent_text": CHORUS, "intent_level": 1})
    assert out == {
        "intent_text": CHORUS,
        "intent_level": 1,
        "intent_op_label": "edit_chorus",
        "intent_op_level": 1,
    }
    out = enrich_export_row({"intent_text": CHORUS})
    assert out["intent_level"] == 3
    assert out["intent_op_level"] == 3


def test_enrich_numeric_intent_text_treated_as_absent():
    out = enrich_export_row({"intent_text": 123})
    assert out["intent_op_level"] == 5
    assert out["intent_op_label"] == "creative_goal"
    assert out["intent_level"] == 5
    # The stored text itself is never rewritten.
    assert out["intent_text"] == 123


@pytest.mark.parametrize("bad", [3.5, ["x"], {"x": 1}, b"x", True])
def test_enrich_nonstring_intent_text_shapes(bad):
    out = enrich_export_row({"intent_text": bad})
    assert out["intent_op_level"] == 5
    assert out["intent_op_label"] == "creative_goal"


def test_enrich_numeric_text_with_valid_level():
    out = enrich_export_row({"intent_text": 123, "intent_level": 3})
    assert out["intent_op_level"] == 3
    assert out["intent_op_label"] == "creative_goal"
    assert out["intent_level"] == 3


def test_enrich_numeric_text_with_string_level():
    out = enrich_export_row({"intent_text": 123, "intent_level": "3"})
    assert out["intent_op_level"] == 5
    assert out["intent_level"] == "3"


def test_enrich_absent_shapes_controls_unchanged():
    assert enrich_export_row({}) == {}
    # Control: falsy text/level never triggered labelling at base.
    assert "intent_op_label" not in enrich_export_row({"intent_text": 0})
    assert "intent_op_label" not in enrich_export_row(
        {"intent_text": "", "intent_level": None}
    )
