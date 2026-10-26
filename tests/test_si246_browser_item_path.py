"""SI-246 tests: get_browser_item non-string path (SI-239 finding F7).

_get_browser_item navigated a caller-supplied path with
path.split("/") and no type check, so an int, list, dict, float or
bytes path failed the whole command with a raw interpreter message
("\x27int\x27 object has no attribute \x27split\x27", "a bytes-like object is
required, not \x27str\x27", ...). The URI side of the same handler was
refused by name in SI-237; the path side was outside that sweep and
no cache key is involved.

The fix reuses the SI-243 _require_string helper - the same helper
_create_audio_clip uses for its path, producing the same refusal
shape, "Invalid path: must be a string" - inside the handler\x27s
existing "if path:" branch, immediately before the split. That
placement is deliberate and pinned here:

* only a truthy value ever reached the split, so only truthy
  non-strings change: they refuse by name instead of crashing raw;
* the omitted path (the dispatcher default is None) and every other
  falsy value (None, "", 0, 0.0, False, [], {}) never reached the
  split and keep their exact prior behaviour - a plain not-found
  result echoing the value;
* a URI hit returns before path navigation, so a valid uri with a
  non-string path beside it keeps its exact prior success.

Neighbour shapes the SI-239 hunt did not enumerate were probed
first-hand at base before this fix: tuple, set, True, NaN and
+/-Infinity are all truthy non-strings and crashed raw at base the
same way; they are refused by name here. Valid paths are
byte-identical controls.

The Remote Script normally runs inside Live, so the fake browser
and instance helpers are the MCP-07 suite\x27s (counting browser).
No Ableton, no network.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tests.test_mcp07_browser_diet import (  # noqa: E402
    WalkCounter, build_browser, make_instance, wire)

MESSAGE = "Invalid path: must be a string"


def _refused_path(inst, bad):
    response = inst._process_command(
        {"type": "get_browser_item", "params": {"path": bad}})
    assert response["status"] == "error", response
    assert response["message"] == MESSAGE
    assert response["result"] == {}


def test_browser_item_nonstring_path_is_refused_by_name():
    # The SI-239 F7 shapes, exactly as probed: int / list / dict /
    # bytes / float. Each runs on a fresh instance; after the named
    # refusal a valid path on the same instance still succeeds.
    for bad in (42, ["instruments"], {"x": 1}, b"instruments", 4.5):
        inst = make_instance(build_browser(WalkCounter()))
        _refused_path(inst, bad)
        result = wire(inst, "get_browser_item",
                      {"path": "instruments/Synths/Analog"})
        assert result["found"] is True
        assert result["item"]["name"] == "Analog"


def test_browser_item_neighbour_shapes_are_refused_by_name():
    # Probed at base, not enumerated by SI-239: tuple, set, bool and
    # the non-finite floats are truthy non-strings and died at the
    # split with the same raw interpreter messages.
    for bad in (("instruments",), {"instruments"}, True,
                float("nan"), float("inf"), float("-inf")):
        inst = make_instance(build_browser(WalkCounter()))
        _refused_path(inst, bad)


def test_browser_item_valid_path_control():
    # Valid paths behave exactly as at base (hand-checked results
    # from the base probe).
    inst = make_instance(build_browser(WalkCounter()))
    result = wire(inst, "get_browser_item",
                  {"path": "instruments/Synths/Analog"})
    assert result == {
        "uri": None, "path": "instruments/Synths/Analog",
        "found": True,
        "item": {"name": "Analog", "is_folder": False,
                 "is_device": True, "is_loadable": True,
                 "uri": "fake:analog"}}
    result = wire(inst, "get_browser_item", {"path": "instruments"})
    assert result["found"] is True
    assert result["item"]["name"] == "Instruments"
    result = wire(inst, "get_browser_item",
                  {"path": "drums/Kits/808 Core Kit"})
    assert result["found"] is True
    assert result["item"]["name"] == "808 Core Kit"


def test_browser_item_omitted_and_falsy_paths_are_unchanged():
    # The dispatcher default for an omitted path is None, and every
    # falsy value skips path navigation at base and after the fix:
    # a plain not-found result echoing the value, never a refusal.
    inst = make_instance(build_browser(WalkCounter()))
    result = wire(inst, "get_browser_item", {})
    assert result == {"uri": None, "path": None, "found": False}
    for value in (None, "", 0, 0.0, False, [], {}):
        inst = make_instance(build_browser(WalkCounter()))
        result = wire(inst, "get_browser_item", {"path": value})
        assert result["found"] is False
        assert result["path"] == value
        assert result["uri"] is None


def test_browser_item_valid_uri_with_bad_path_is_unchanged():
    # A URI hit returns before path navigation at base and after
    # the fix, so the path value beside a found uri is never read.
    inst = make_instance(build_browser(WalkCounter()))
    result = wire(inst, "get_browser_item",
                  {"uri": "fake:808", "path": 42})
    assert result["found"] is True
    assert result["item"]["name"] == "808 Core Kit"
    assert result["path"] == 42
