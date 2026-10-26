"""SI-249 tests: _process_command non-dict command (SI-239 F10).

_process_command assumed a dict command and escaped with a raw
AttributeError ("'list' object has no attribute 'get'", ...) for a
non-dict command, instead of returning the dispatcher's own entry
error envelope. Every current wire path happens to close this
(v1 first-byte sniff refuses a non-"{" first byte, v2 checks
isinstance(command, dict), batch items are guarded, the MCP server
builds the dict), so this is defence-in-depth for direct callers
and future dispatch paths only.

The fix returns the entry error envelope the dispatcher already
uses for entry-level errors (the "Invalid params: must be an
object" / "Invalid command type: must be a string" shape:
{"status": "error", "message": ...}, no result key), with the
message "Invalid command: must be an object", before any .get on
the command. Dict commands are byte-identical controls.

The Remote Script normally runs inside Live, so _Framework is
stubbed and the class is instantiated without __init__ (the
mcp01 pattern). No Ableton, no network.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLED_SCRIPT = (REPO_ROOT / "MCP_Server" / "bundled_ableton_remote_script"
                  / "AbletonMCP_init.py")

ENVELOPE = {"status": "error", "message": "Invalid command: must be an object"}


def _load_script_module():
    framework = types.ModuleType("_Framework")
    control_surface_mod = types.ModuleType("_Framework.ControlSurface")

    class _StubControlSurface(object):
        def __init__(self, *args, **kwargs):
            pass

        def log_message(self, *args, **kwargs):
            pass

        def show_message(self, *args, **kwargs):
            pass

    control_surface_mod.ControlSurface = _StubControlSurface
    framework.ControlSurface = control_surface_mod
    sys.modules.setdefault("_Framework", framework)
    sys.modules.setdefault("_Framework.ControlSurface", control_surface_mod)
    spec = importlib.util.spec_from_file_location(
        "ableton_mcp_remote_script_si249", BUNDLED_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script_module():
    return _load_script_module()


def make_instance(script_module):
    inst = script_module.AbletonMCP.__new__(script_module.AbletonMCP)
    inst.log_message = lambda *a, **k: None
    inst.show_message = lambda *a, **k: None
    inst.schedule_message = lambda delay, fn: fn()
    return inst


@pytest.mark.parametrize("bad", [
    ["x"],          # E0 list
    "str",          # E1 string
    None,           # E2 None
    42,             # E3 int
    True,           # E4 bool
    4.5,            # E5 float
    ("x",),         # neighbour: tuple, probed at base, same escape
    {1},            # neighbour: set, probed at base, same escape
    b"x",           # neighbour: bytes, probed at base, same escape
])
def test_non_dict_command_returns_entry_error_envelope(script_module, bad):
    inst = make_instance(script_module)
    called = []
    inst._get_session_info = lambda: called.append(1) or {"tempo": 120.0}
    assert inst._process_command(bad) == ENVELOPE
    assert called == []


def test_refusal_then_dict_command_still_dispatches(script_module):
    inst = make_instance(script_module)
    inst._get_session_info = lambda: {"tempo": 120.0}
    assert inst._process_command(["x"]) == ENVELOPE
    assert inst._process_command(
        {"type": "get_session_info", "params": {}}) == {
            "status": "success", "result": {"tempo": 120.0}}


def test_dict_controls_byte_identical(script_module):
    inst = make_instance(script_module)
    inst._get_session_info = lambda: {"tempo": 120.0}
    assert inst._process_command(
        {"type": "get_session_info", "params": {}}) == {
            "status": "success", "result": {"tempo": 120.0}}
    assert inst._process_command({"params": {}}) == {
        "status": "error", "result": {}, "message": "Unknown command: "}
    assert inst._process_command({"type": "nope_cmd", "params": {}}) == {
        "status": "error", "result": {},
        "message": "Unknown command: nope_cmd"}
    assert inst._process_command({"type": "get_session_info"}) == {
        "status": "success", "result": {"tempo": 120.0}}


def test_existing_entry_guards_byte_identical(script_module):
    inst = make_instance(script_module)
    assert inst._process_command({"type": "set_tempo", "params": [1, 2]}) == {
        "status": "error", "message": "Invalid params: must be an object"}
    assert inst._process_command({"type": 42, "params": {}}) == {
        "status": "error",
        "message": "Invalid command type: must be a string"}
