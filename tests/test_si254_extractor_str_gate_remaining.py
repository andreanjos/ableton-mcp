"""SI-254 -- extractor str-gate fall-through, remaining string-typed keys.

SI-253 closed free-text and uri-type keys, but the same isinstance(value,
str) fall-through still stored non-string values raw for the remaining
string-typed keys in the same two extractors: trajectory rating / winner /
candidate_a / candidate_b / category_type, telemetry category_type, and
trajectory tags. Non-string values for the plain-string keys are now
dropped: string values are stored by design, and a non-string has no
legitimate form -- the SI-253 free-text/uri drop is the precedent.

Tags ruling (probe-first, SI-253 review section 6): tags is a
comma-separated string at the tool boundary (rate_last_action(tags: str))
but list[str] in the recorder schema (schema.make_preference /
recorder.record_preference -- the server itself splits the string into a
list before recording). A list whose items are all strings is therefore a
legitimate form and is stored unchanged; every other non-string (bytes
included, tuples included, and any list carrying a non-string item) is
legitimate under neither reading and is dropped.

String controls -- and the list-of-strings tags control -- are
byte-identical to base in every family. Numeric/bool-typed keys are out
of scope (their legitimate values are non-strings; SI-253 review S6).
"""
import pytest

from MCP_Server.dataset.trajectory_decorator import _extract_params
from MCP_Server.telemetry_decorator import _extract_tool_params

TRAJ_STR_KEYS = ["rating", "winner", "candidate_a", "candidate_b", "category_type"]
NONSTRING_VALUES = [
    b"hello secret /secret/kit",
    bytearray(b"hello secret"),
    ["groove", "harmony"],
    {"x": "secret"},
    123,
    1.5,
    True,
    {"groove"},
    ("groove", "harmony"),
]


# -- Trajectory plain-string keys: non-strings dropped, never raw -----------

@pytest.mark.parametrize("key", TRAJ_STR_KEYS)
@pytest.mark.parametrize("value", NONSTRING_VALUES)
def test_traj_nonstring_str_key_is_dropped_not_stored_verbatim(key, value):
    assert _extract_params({key: value}) == {}


@pytest.mark.parametrize("key", TRAJ_STR_KEYS)
def test_traj_string_str_key_control_is_stored_verbatim(key):
    assert _extract_params({key: "better"}) == {key: "better"}


@pytest.mark.parametrize("key", TRAJ_STR_KEYS)
def test_traj_string_str_key_long_control_is_truncated_byte_identically(key):
    assert _extract_params({key: "x" * 600}) == {key: "x" * 500 + "..."}


# -- Telemetry category_type: non-strings dropped, strings byte-identical ---

@pytest.mark.parametrize("value", NONSTRING_VALUES)
def test_tele_nonstring_category_type_is_dropped_not_stored_verbatim(value):
    assert _extract_tool_params({"category_type": value}) == {}


def test_tele_string_category_type_control_is_captured_verbatim():
    assert _extract_tool_params({"category_type": "instruments"}) == {"category_type": "instruments"}


def test_tele_string_category_type_long_control_is_truncated_byte_identically():
    assert _extract_tool_params({"category_type": "x" * 600}) == {"category_type": "x" * 500 + "..."}


# -- Trajectory tags: illegitimate shapes dropped ----------------------------

TAGS_DROPPED_VALUES = [
    b"groove,harmony",
    bytearray(b"groove"),
    ["groove", 123],
    [b"groove"],
    {"x": "groove"},
    123,
    1.5,
    True,
    {"groove"},
    ("groove", "harmony"),
]


@pytest.mark.parametrize("value", TAGS_DROPPED_VALUES)
def test_traj_tags_illegitimate_shape_is_dropped_not_stored_verbatim(value):
    assert _extract_params({"tags": value}) == {}


# -- Trajectory tags: legitimate shapes unchanged ----------------------------

def test_traj_tags_string_control_is_stored_verbatim():
    assert _extract_params({"tags": "groove,harmony"}) == {"tags": "groove,harmony"}


def test_traj_tags_string_long_control_is_truncated_byte_identically():
    assert _extract_params({"tags": "x" * 600}) == {"tags": "x" * 500 + "..."}


def test_traj_tags_list_of_strings_control_is_stored_unchanged():
    # The recorder-schema shape: list[str] is legitimate for tags (ruling
    # in the module docstring above) and must survive byte-identically.
    assert _extract_params({"tags": ["groove", "harmony"]}) == {"tags": ["groove", "harmony"]}


def test_traj_tags_empty_list_control_is_stored_unchanged():
    assert _extract_params({"tags": []}) == {"tags": []}
