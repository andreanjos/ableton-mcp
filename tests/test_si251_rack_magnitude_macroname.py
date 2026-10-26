"""SI-251 tests: map_rack_magnitude macro_name validation.

SI-243 review N1: _map_rack_magnitude forwarded its macro_name
unvalidated - non-string values reached rack.rename_macro(0,
macro_name) verbatim on the primary, unguarded path (and
rack.parameters[1].name = macro_name on the fallback path) and
echoed as success, proven at base and at the SI-243 head alike
(list/123/None probed there; dict probed by this row's builder).

The fix routes macro_name through the existing SI-243
_require_string validator at the handler entry, before both the
primary and fallback paths. These pins cover it at the dispatcher
level against a fake rack, in the suite pattern: every probed bad
shape refuses by name with no rename call / no fallback assignment,
and the valid-string + omitted-default ("Magnitude") controls behave
exactly as before, on both paths.

The Remote Script normally runs inside Live, so _Framework is
stubbed and the class is instantiated without __init__ (the
bridge-test pattern). No Ableton, no network.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLED_SCRIPT = (REPO_ROOT / "MCP_Server" / "bundled_ableton_remote_script"
                  / "AbletonMCP_init.py")

MESSAGE = "Invalid macro_name: must be a string"
BAD = [["x"], 123, None, {"x": 1}]


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
        "ableton_mcp_remote_script_si251", BUNDLED_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script_module():
    return _load_script_module()


# ---------------------------------------------------------------- fake LOM

class FakeParam(object):
    def __init__(self, name):
        self.name = name


class FakeRackPrimary(object):
    """A rack with rename_macro (the primary naming path)."""

    def __init__(self):
        self.name = "Rack1"
        self.can_have_chains = True
        self.visible_macro_count = 1
        self.chains = []
        self.parameters = [FakeParam("Device On"), FakeParam("Macro 1")]
        self.macros_mapped = []
        self.rename_calls = []
        self.macro_map_calls = []

    def rename_macro(self, index, name):
        self.rename_calls.append((index, name))

    def macro_map(self, index, param):
        self.macro_map_calls.append((index, param))


class FakeRackFallback(object):
    """A rack without rename_macro (the parameters[1].name fallback)."""

    def __init__(self):
        self.name = "Rack1"
        self.can_have_chains = True
        self.visible_macro_count = 1
        self.chains = []
        self.parameters = [FakeParam("Device On"), FakeParam("Macro 1")]
        self.macros_mapped = []
        self.macro_map_calls = []

    def macro_map(self, index, param):
        self.macro_map_calls.append((index, param))


class FakeTrack(object):
    def __init__(self, rack):
        self.name = "T0"
        self.devices = [rack]


class FakeSong(object):
    def __init__(self, track):
        self.tracks = [track]


def make_instance(script_module, rack):
    inst = script_module.AbletonMCP.__new__(script_module.AbletonMCP)
    inst._song = FakeSong(FakeTrack(rack))
    inst.log_message = lambda *a, **k: None
    inst.show_message = lambda *a, **k: None
    inst.schedule_message = lambda delay, fn: fn()
    return inst


def wire(inst, **params):
    return inst._process_command(
        {"type": "map_rack_magnitude", "params": params})


def assert_refused(response):
    assert response["status"] == "error", response
    assert response["message"] == MESSAGE, response


# ------------------------------------------------------------------- primary

def test_primary_bad_macro_names_refused_by_name_no_rename(script_module):
    for value in BAD:
        rack = FakeRackPrimary()
        inst = make_instance(script_module, rack)
        response = wire(inst, track_index=0, device_index=0,
                        macro_name=value)
        assert_refused(response)
        assert rack.rename_calls == [], value
        assert rack.macro_map_calls == [], value


def test_fallback_bad_macro_names_refused_by_name_no_assignment(
        script_module):
    for value in BAD:
        rack = FakeRackFallback()
        inst = make_instance(script_module, rack)
        response = wire(inst, track_index=0, device_index=0,
                        macro_name=value)
        assert_refused(response)
        assert rack.parameters[1].name == "Macro 1", value
        assert rack.macro_map_calls == [], value


# ------------------------------------------------------------------ controls

def test_primary_valid_macro_name_unchanged(script_module):
    rack = FakeRackPrimary()
    inst = make_instance(script_module, rack)
    response = wire(inst, track_index=0, device_index=0, macro_name="Mag2")
    assert response["status"] == "success", response
    assert rack.rename_calls == [(0, "Mag2")]
    assert response["result"]["macro_name"] == "Mag2"
    # The empty string is a string: unchanged.
    rack = FakeRackPrimary()
    inst = make_instance(script_module, rack)
    response = wire(inst, track_index=0, device_index=0, macro_name="")
    assert response["status"] == "success", response
    assert rack.rename_calls == [(0, "")]
    assert response["result"]["macro_name"] == ""


def test_primary_omitted_macro_name_default_unchanged(script_module):
    rack = FakeRackPrimary()
    inst = make_instance(script_module, rack)
    response = inst._process_command(
        {"type": "map_rack_magnitude",
         "params": {"track_index": 0, "device_index": 0}})
    assert response["status"] == "success", response
    assert rack.rename_calls == [(0, "Magnitude")]
    assert response["result"]["macro_name"] == "Magnitude"


def test_fallback_valid_and_default_macro_name_unchanged(script_module):
    rack = FakeRackFallback()
    inst = make_instance(script_module, rack)
    response = wire(inst, track_index=0, device_index=0, macro_name="Mag2")
    assert response["status"] == "success", response
    assert rack.parameters[1].name == "Mag2"
    assert response["result"]["macro_name"] == "Mag2"

    rack = FakeRackFallback()
    inst = make_instance(script_module, rack)
    response = inst._process_command(
        {"type": "map_rack_magnitude",
         "params": {"track_index": 0, "device_index": 0}})
    assert response["status"] == "success", response
    assert rack.parameters[1].name == "Magnitude"
    assert response["result"]["macro_name"] == "Magnitude"
