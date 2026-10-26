"""SI-253 -- extractor free-text / uri bytes scrub bypass (SI-248 D6 + N2).

SI-248 covered path/name, but the same str-gate fall-through stored other
non-string values raw: trajectory free-text keys (text / note /
search_query) passed bytes through verbatim, bypassing scrub_text
entirely, and uri-type keys (uri / rack_uri / kit_path) stored bytes
verbatim in BOTH extractors, including telemetry kit_path storing
b"/secret/kit" raw. Non-string values for these keys are now dropped:
string values are stored (free text scrubbed, URIs raw with consent) by
design, and a non-string has no consented string form -- the telemetry
non-string name drop from SI-248 is the precedent. Also folded in: the
SI-248 name placeholder len() guard now catches Exception, not only
TypeError (SI-248 review N2).

String controls are byte-identical to base in every family.
"""
import pytest

from MCP_Server.dataset.trajectory_decorator import _extract_params
from MCP_Server.telemetry_decorator import _extract_tool_params

FREE_TEXT_KEYS = ["text", "note", "search_query"]
URI_KEYS = ["uri", "rack_uri", "kit_path"]
NONSTRING_VALUES = [
    b"hello secret /secret/kit",
    bytearray(b"hello secret"),
    ["hello secret"],
    {"x": "secret"},
    123,
]
SECRET_BYTES = b"/secret/kit"


# -- Trajectory free-text: non-strings dropped, never stored raw ------------

@pytest.mark.parametrize("key", FREE_TEXT_KEYS)
@pytest.mark.parametrize("value", NONSTRING_VALUES)
def test_traj_nonstring_free_text_is_dropped_not_stored_verbatim(key, value):
    assert _extract_params({key: value}) == {}


@pytest.mark.parametrize("key", FREE_TEXT_KEYS)
def test_traj_string_free_text_control_is_scrubbed_byte_identically(key):
    params = _extract_params({key: "like /Users/ariadne/Music/kick.wav but warmer send to ariadne@example.com"})
    assert params == {key: "like <path> but warmer send to <email>"}


@pytest.mark.parametrize("key", FREE_TEXT_KEYS)
def test_traj_string_free_text_plain_control_is_kept(key):
    assert _extract_params({key: "hello world"}) == {key: "hello world"}


# -- Trajectory uri-type: non-strings dropped, strings byte-identical -------

@pytest.mark.parametrize("key", URI_KEYS)
@pytest.mark.parametrize("value", NONSTRING_VALUES)
def test_traj_nonstring_uri_is_dropped_not_stored_verbatim(key, value):
    assert _extract_params({key: value}) == {}


@pytest.mark.parametrize("key", URI_KEYS)
def test_traj_bytes_kit_path_shape_from_d6_is_dropped(key):
    # The exact reviewer-probed shape family: a path-shaped bytes value.
    assert _extract_params({key: SECRET_BYTES}) == {}


@pytest.mark.parametrize("key", URI_KEYS)
def test_traj_string_uri_control_is_stored_verbatim(key):
    assert _extract_params({key: "ableton:instrument:123"}) == {key: "ableton:instrument:123"}


@pytest.mark.parametrize("key", URI_KEYS)
def test_traj_string_uri_long_control_is_truncated_byte_identically(key):
    assert _extract_params({key: "x" * 600}) == {key: "x" * 500 + "..."}


# -- Telemetry uri-type: non-strings dropped, strings byte-identical --------

@pytest.mark.parametrize("key", URI_KEYS)
@pytest.mark.parametrize("value", NONSTRING_VALUES)
def test_tele_nonstring_uri_is_dropped_not_stored_verbatim(key, value):
    assert _extract_tool_params({key: value}) == {}


@pytest.mark.parametrize("key", URI_KEYS)
def test_tele_bytes_kit_path_shape_from_d6_is_dropped(key):
    assert _extract_tool_params({key: SECRET_BYTES}) == {}


@pytest.mark.parametrize("key", URI_KEYS)
def test_tele_string_uri_control_is_captured_verbatim(key):
    assert _extract_tool_params({key: "ableton:instrument:123"}) == {key: "ableton:instrument:123"}


@pytest.mark.parametrize("key", URI_KEYS)
def test_tele_string_uri_long_control_is_truncated_byte_identically(key):
    assert _extract_tool_params({key: "x" * 600}) == {key: "x" * 500 + "..."}


# -- SI-248 N2: the name placeholder len() guard catches ValueError too -----

class _BadLenValueError:
    def __len__(self):
        raise ValueError("boom")


def test_traj_name_with_valueerror_len_gets_zero_placeholder():
    assert _extract_params({"name": _BadLenValueError()}) == {"name": "<name:0>"}
