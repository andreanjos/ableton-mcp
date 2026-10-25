"""MCP-06 tests: subset device-parameter reads + set_device_parameters.

Pins:

* subset reads - a subsetted get_device_parameters returns exactly
  the requested slice of the full read (by indices, by names, or the
  union of both), with per-entry shape unchanged, at every level of
  the device tree; an unsubsetted call is exactly the pre-subset read;
* the single set is the oracle - a bulk set's per-item envelopes equal
  what sequential direct set_device_parameter calls produce on an
  identical fake device (same clamping, same results, same errors),
  and the two runs leave the device in identical states;
* one scheduling - a bulk set of 8 items costs exactly one
  schedule_message hop, including inside an execute_batch;
* per-item failure isolation - a bad index, an unknown name, a
  malformed item: per-item errors, the other items still apply;
  stop_on_error reports the rest as skipped, never silently dropped;
* whole-command validation - missing/non-list/oversized parameter
  lists are command-level errors that schedule nothing;
* the tool surface - subset selectors are forwarded only when set and
  the new tool sends exactly its documented params, so a default call
  is byte-identical to the pre-MCP-06 wire shape;
* graceful degradation - a peer that ignores the subset selectors
  returns the full read, and the new command is advertised in
  SCRIPT_CAPABILITIES at the bumped script version.

The Remote Script normally runs inside Live, so _Framework is stubbed
and the class is instantiated without __init__ (the bridge-test
pattern). The fake parameters clamp on assignment, as Live's
DeviceParameter does, so clamping is part of the oracle comparison.
No Ableton, no network (except one loopback-free subprocess).
"""

import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLED_SCRIPT = (REPO_ROOT / "MCP_Server" / "bundled_ableton_remote_script"
                  / "AbletonMCP_init.py")


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
        "ableton_mcp_remote_script_mcp06", BUNDLED_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script_module():
    return _load_script_module()


# ---------------------------------------------------------------- fake LOM

class FakeParam(object):
    """A Live DeviceParameter stand-in: assignment clamps to [min, max]."""

    def __init__(self, name, value=0.5, minimum=0.0, maximum=1.0):
        self.name = name
        self.min = minimum
        self.max = maximum
        self.is_enabled = True
        self.is_quantized = False
        self.automation_state = 0
        self._value = minimum
        self.value = value

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, new_value):
        self._value = min(max(float(new_value), self.min), self.max)
        self.value_string = "%.2f" % self._value


class FakeDevice(object):
    def __init__(self, name, params, chains=None):
        self.name = name
        self.class_name = "Operator"
        self.class_display_name = "Instrument"
        self.can_have_drum_pads = False
        self.can_have_chains = chains is not None
        self.parameters = params
        if chains is not None:
            self.chains = chains


class FakeMixer(object):
    def __init__(self):
        self.volume = FakeParam("Volume", 0.85)
        self.panning = FakeParam("Pan", 0.0, -1.0, 1.0)


class FakeChain(object):
    def __init__(self, name, devices):
        self.name = name
        self.mute = False
        self.solo = False
        self.mixer_device = FakeMixer()
        self.devices = devices


class FakeTrack(object):
    def __init__(self, devices):
        self.devices = devices


class FakeSong(object):
    def __init__(self, tracks):
        self.tracks = tracks


PARAM_NAMES = ["Osc A Level", "Osc B Level", "Filter Cutoff",
               "Filter Resonance", "Amp Attack", "Amp Decay",
               "Amp Sustain", "Amp Release", "LFO Rate", "LFO Amount",
               "Chorus Mix", "Reverb Mix"]


def make_device(param_count=12):
    return FakeDevice("Synth", [
        FakeParam(PARAM_NAMES[i], value=0.05 * (i + 1))
        for i in range(param_count)])


def make_song(param_count=12):
    return FakeSong([FakeTrack([make_device(param_count)])])


def make_rack_song():
    nested = FakeDevice("Nested", [FakeParam("Nested Gain", 0.3),
                                   FakeParam("Nested Tone", 0.7)])
    rack = FakeDevice("Rack", [FakeParam("Macro 1", 0.1),
                               FakeParam("Macro 2", 0.2)],
                      chains=[FakeChain("Chain 1", [nested])])
    return FakeSong([FakeTrack([rack])])


def make_instance(script_module, song=None):
    inst = script_module.AbletonMCP.__new__(script_module.AbletonMCP)
    inst._song = song if song is not None else make_song()
    inst.log_message = lambda *a, **k: None
    inst.show_message = lambda *a, **k: None
    inst.schedule_calls = []

    def schedule(delay, fn):
        inst.schedule_calls.append(delay)
        fn()
    inst.schedule_message = schedule
    return inst


def device_params_via_wire(inst, **extra_params):
    params = {"track_index": 0, "device_index": 0}
    params.update(extra_params)
    response = inst._process_command(
        {"type": "get_device_parameters", "params": params})
    assert response["status"] == "success", response
    return response["result"]["device"]["parameters"]


def single_set_via_wire(inst, parameter_index, value):
    return inst._process_command({
        "type": "set_device_parameter",
        "params": {"track_index": 0, "device_index": 0,
                   "parameter_index": parameter_index, "value": value}})


def bulk_set_via_wire(inst, items, **extra_params):
    params = {"track_index": 0, "device_index": 0, "parameters": items}
    params.update(extra_params)
    return inst._process_command(
        {"type": "set_device_parameters", "params": params})


# ------------------------------------------------------------- subset reads

def test_subset_by_indices_is_exact_slice_of_full(script_module):
    inst = make_instance(script_module)
    full = device_params_via_wire(inst)
    assert len(full) == 12
    subset = device_params_via_wire(inst, parameter_indices=[7, 2, 2, 99])
    # Song order preserved, duplicates and unmatched selectors ignored.
    assert subset == [e for e in full if e["index"] in (2, 7)]
    assert [e["name"] for e in subset] == ["Filter Cutoff", "Amp Release"]


def test_subset_by_names_and_union(script_module):
    inst = make_instance(script_module)
    full = device_params_via_wire(inst)
    by_name = device_params_via_wire(
        inst, parameter_names=["LFO Rate", "No Such Param"])
    assert by_name == [e for e in full if e["name"] == "LFO Rate"]
    union = device_params_via_wire(
        inst, parameter_indices=[0], parameter_names=["Reverb Mix"])
    assert union == [e for e in full
                     if e["index"] == 0 or e["name"] == "Reverb Mix"]


def test_subset_empty_selection_returns_no_parameters(script_module):
    inst = make_instance(script_module)
    assert device_params_via_wire(inst, parameter_indices=[]) == []
    assert device_params_via_wire(inst, parameter_names=[]) == []


def test_subset_parameter_indices_infinity_entries_are_skipped(script_module):
    # SI-240: int(+-inf) raises OverflowError, which the selector's
    # except (TypeError, ValueError) did not catch, so a +-Infinity
    # entry failed the whole read with "cannot convert float
    # infinity to integer" instead of being skipped like every
    # other unusable entry (SI-239 F1, sibling of the SI-236 fix).
    inst = make_instance(script_module)
    assert device_params_via_wire(inst, parameter_indices=[float("inf")]) == []
    assert device_params_via_wire(inst, parameter_indices=[float("-inf")]) == []
    assert device_params_via_wire(
        inst, parameter_indices=[float("inf"), float("-inf")]) == []


def test_subset_parameter_indices_nan_entry_is_skipped_control(script_module):
    # Control: int(NaN) raises ValueError, already caught at base,
    # so a NaN entry was already skipped; this pin must pass both
    # before and after the SI-240 fix.
    inst = make_instance(script_module)
    assert device_params_via_wire(inst, parameter_indices=[float("nan")]) == []


def test_subset_parameter_indices_mixed_valid_and_infinity_returns_valid(script_module):
    # One bad entry must not poison an otherwise valid list:
    # the valid selection is still returned.
    inst = make_instance(script_module)
    full = device_params_via_wire(inst)
    mixed = device_params_via_wire(
        inst, parameter_indices=[1, float("inf")])
    assert mixed == [e for e in full if e["index"] == 1]
    mixed2 = device_params_via_wire(
        inst, parameter_indices=[0, float("-inf"), 2])
    assert mixed2 == [e for e in full if e["index"] in (0, 2)]


def device_params_response(inst, **extra_params):
    params = {"track_index": 0, "device_index": 0}
    params.update(extra_params)
    return inst._process_command(
        {"type": "get_device_parameters", "params": params})


def test_subset_parameter_indices_non_list_selector_refused_by_name(
        script_module):
    # SI-241: a non-list parameter_indices was silently misread - a
    # string was iterated character-wise ("12" selected parameters 1
    # and 2), a dict iterated its keys, a set iterated its members -
    # or died raw ("'int' object is not iterable" for 123). A selector
    # that is not a list/tuple can never be honoured as sent, so it
    # refuses by name (SI-239 F2, SI-237 entry-refusal style).
    inst = make_instance(script_module)
    for bad in ("12", {"1": 9, "2": 9}, 123, 1.5, True, {1, 2}):
        response = device_params_response(inst, parameter_indices=bad)
        assert response["status"] == "error", bad
        assert response["message"] == \
            "Invalid parameter_indices: must be a list or tuple", bad


def test_subset_parameter_indices_wrong_type_entries_are_skipped(
        script_module):
    # SI-241: entries that are not real ints never select - a float
    # is no longer truncated ([1.9, 0.9] selected 1 and 0), True no
    # longer selects index 1, False no longer selects index 0, and a
    # digit string is no longer coerced (["1", "2"] selected 1 and
    # 2). Wrong-type entries are skipped, preserving the established
    # skip-unusable-entries contract ("x"/None/[[1]]/NaN/+-Inf are
    # skipped, pinned above and by SI-240): one bad entry does not
    # fail the whole read.
    inst = make_instance(script_module)
    assert device_params_via_wire(inst, parameter_indices=[1.9, 0.9]) == []
    assert device_params_via_wire(inst, parameter_indices=[True]) == []
    assert device_params_via_wire(inst, parameter_indices=[False]) == []
    assert device_params_via_wire(inst, parameter_indices=["1", "2"]) == []
    full = device_params_via_wire(inst)
    mixed = device_params_via_wire(inst, parameter_indices=[1, 0.9])
    assert mixed == [e for e in full if e["index"] == 1]
    mixed2 = device_params_via_wire(inst, parameter_indices=[2, True])
    assert mixed2 == [e for e in full if e["index"] == 2]
    mixed3 = device_params_via_wire(inst, parameter_indices=[0, "2"])
    assert mixed3 == [e for e in full if e["index"] == 0]


def test_subset_parameter_indices_tuple_selector_still_works(script_module):
    # Control: a tuple selector was valid before SI-241 and stays
    # valid - only non-list/tuple containers change.
    inst = make_instance(script_module)
    full = device_params_via_wire(inst)
    subset = device_params_via_wire(inst, parameter_indices=(1, 2))
    assert subset == [e for e in full if e["index"] in (1, 2)]


def test_subset_parameter_names_non_list_selector_refused_by_name(
        script_module):
    # SI-241: parameter_names="Filter Cutoff" (a string) silently
    # returned a successful read with zero parameters, a dict
    # iterated its keys and could actually select, and 123 died raw
    # ("'int' object is not iterable"). Non-list/tuple selectors
    # refuse by name.
    inst = make_instance(script_module)
    for bad in ("Filter Cutoff", 123, {"Filter Cutoff": 1},
                {"Filter Cutoff"}, 1.5, True):
        response = device_params_response(inst, parameter_names=bad)
        assert response["status"] == "error", bad
        assert response["message"] == \
            "Invalid parameter_names: must be a list or tuple", bad


def test_subset_parameter_names_non_string_entries_refused_by_name(
        script_module):
    # SI-241: an unhashable name entry crashed raw ("unhashable
    # type: 'list'") and a hashable non-string silently matched
    # nothing. Name entries must be strings; any other entry refuses
    # by name, even alongside a valid name - the read already failed
    # whole for the unhashable shapes at base, so a by-name refusal
    # is not a new failure mode here.
    inst = make_instance(script_module)
    for bad in ([["Filter Cutoff"]], [{"a": 1}], [123], [None], [True],
                [1.5], [("Filter Cutoff",)], [b"Filter Cutoff"],
                ["Filter Cutoff", ["x"]], ["Filter Cutoff", 123]):
        response = device_params_response(inst, parameter_names=bad)
        assert response["status"] == "error", bad
        assert response["message"] == \
            "Invalid parameter_names: entries must be strings", bad


def test_subset_parameter_names_tuple_selector_still_works(script_module):
    # Control: a tuple of names was valid before SI-241 and stays
    # valid - only non-list/tuple containers change.
    inst = make_instance(script_module)
    by_name = device_params_via_wire(
        inst, parameter_names=("Filter Cutoff",))
    assert [e["name"] for e in by_name] == ["Filter Cutoff"]


def test_unsubsetted_read_is_the_pre_subset_read(script_module):
    inst = make_instance(script_module)
    via_wire = inst._process_command(
        {"type": "get_device_parameters",
         "params": {"track_index": 0, "device_index": 0}})
    assert via_wire["result"] == inst._get_device_parameters(0, 0)
    full = via_wire["result"]["device"]["parameters"]
    assert [e["index"] for e in full] == list(range(12))
    assert full[0]["name"] == "Osc A Level"


def test_subset_reaches_nested_chain_devices(script_module):
    inst = make_instance(script_module, make_rack_song())
    full = inst._process_command(
        {"type": "get_device_parameters",
         "params": {"track_index": 0, "device_index": 0}})["result"]
    subset = inst._process_command(
        {"type": "get_device_parameters",
         "params": {"track_index": 0, "device_index": 0,
                    "parameter_names": ["Macro 2", "Nested Tone"]}}
    )["result"]
    assert [e["name"] for e in subset["device"]["parameters"]] == ["Macro 2"]
    nested_full = full["device"]["chains"][0]["devices"][0]["parameters"]
    nested_sub = subset["device"]["chains"][0]["devices"][0]["parameters"]
    assert nested_sub == [e for e in nested_full if e["name"] == "Nested Tone"]


def test_subset_ignored_by_peer_without_the_form(script_module):
    """A peer whose read ignores the selectors returns the full read:
    subsetting degrades to today's behaviour, never to an error."""
    inst = make_instance(script_module)
    real_impl = inst._get_device_parameters

    def legacy_impl(track_index, device_index, **_ignored):
        return real_impl(track_index, device_index)

    inst._get_device_parameters = legacy_impl
    subset = device_params_via_wire(inst, parameter_indices=[1])
    assert len(subset) == 12


# ------------------------------------------------------------- bulk setting

TUNE_ITEMS = [
    {"parameter_index": 2, "value": 0.75},     # plain set
    {"parameter_index": 3, "value": 7.5},      # clamps to max (1.0)
    {"parameter_index": 4, "value": -2.0},     # clamps to min (0.0)
    {"name": "LFO Rate", "value": 0.33},       # name-addressed
    {"parameter_index": 99, "value": 0.5},      # bad index
    {"name": "No Such Param", "value": 0.5},   # unknown name
    "not-an-object",                           # malformed item
    {"value": 0.5},                            # no address
    {"parameter_index": 10, "value": 0.9},     # still applies afterwards
]


def _oracle_envelope(inst, item):
    """What sequential direct calls produce for one bulk item."""
    if not isinstance(item, dict):
        return None  # no direct-call equivalent; asserted as error below
    if item.get("parameter_index") is not None:
        return single_set_via_wire(inst, item["parameter_index"],
                                   item.get("value", 0.0))
    if item.get("name") is not None:
        device = inst._song.tracks[0].devices[0]
        for index, param in enumerate(device.parameters):
            if param.name == item["name"]:
                return single_set_via_wire(inst, index,
                                           item.get("value", 0.0))
        return None  # unknown name: no direct-call equivalent
    return None


def test_bulk_set_matches_sequential_single_sets(script_module):
    bulk_inst = make_instance(script_module)
    oracle_inst = make_instance(script_module)

    response = bulk_set_via_wire(bulk_inst, TUNE_ITEMS)
    assert response["status"] == "success", response
    result = response["result"]
    assert result["summary"] == {"total": 9, "succeeded": 5,
                                 "failed": 4, "skipped": 0}

    for item, item_response in zip(TUNE_ITEMS, result["results"]):
        oracle = _oracle_envelope(oracle_inst, item)
        if oracle is None:
            assert item_response["status"] == "error", (item, item_response)
        else:
            assert item_response == oracle, (item, item_response, oracle)

    # Error messages name the failure the single set would have raised.
    assert result["results"][4]["message"] == "Parameter index out of range"
    assert result["results"][5]["message"] == \
        "Parameter not found: No Such Param"

    # Both runs leave their devices in identical states.
    bulk_values = [p.value for p in
                   bulk_inst._song.tracks[0].devices[0].parameters]
    oracle_values = [p.value for p in
                     oracle_inst._song.tracks[0].devices[0].parameters]
    assert bulk_values == oracle_values
    assert bulk_values[2] == 0.75
    assert bulk_values[3] == 1.0    # clamped, exactly as the single set
    assert bulk_values[4] == 0.0    # clamped
    assert bulk_values[8] == 0.33   # name-addressed
    assert bulk_values[10] == 0.9   # applied after the failures


def test_bulk_set_runs_in_one_scheduling(script_module):
    inst = make_instance(script_module)
    items = [{"parameter_index": i, "value": 0.5} for i in range(8)]
    response = bulk_set_via_wire(inst, items)
    assert response["result"]["summary"]["succeeded"] == 8
    # THE point of the feature: one main-thread hop for all 8 writes,
    # where 8 direct set_device_parameter calls would hop 8 times.
    assert len(inst.schedule_calls) == 1


def test_bulk_set_inside_batch_still_one_scheduling(script_module):
    inst = make_instance(script_module)
    items = [{"parameter_index": i, "value": 0.25} for i in range(3)]
    response = inst._process_command({
        "type": "execute_batch",
        "params": {"commands": [
            {"type": "set_device_parameters",
             "params": {"track_index": 0, "device_index": 0,
                        "parameters": items}},
        ]}})
    assert response["status"] == "success", response
    item = response["result"]["results"][0]
    assert item["status"] == "success"
    assert item["result"]["summary"]["succeeded"] == 3
    assert len(inst.schedule_calls) == 1


def test_bulk_set_stop_on_error_skips_the_rest(script_module):
    inst = make_instance(script_module)
    items = [{"parameter_index": 0, "value": 0.11},
             {"parameter_index": 99, "value": 0.5},
             {"parameter_index": 1, "value": 0.22}]
    response = bulk_set_via_wire(inst, items, stop_on_error=True)
    result = response["result"]
    assert [r["status"] for r in result["results"]] == \
        ["success", "error", "skipped"]
    assert result["summary"] == {"total": 3, "succeeded": 1,
                                 "failed": 1, "skipped": 1}
    params = inst._song.tracks[0].devices[0].parameters
    assert params[0].value == 0.11
    assert params[1].value == 0.10  # untouched: skipped, not applied


def test_bulk_set_validation_is_command_level(script_module):
    inst = make_instance(script_module)
    for bad_params in ({"track_index": 0, "device_index": 0},
                       {"track_index": 0, "device_index": 0,
                        "parameters": "not-a-list"}):
        response = inst._process_command(
            {"type": "set_device_parameters", "params": bad_params})
        assert response["status"] == "error"
        assert "requires a 'parameters' list" in response["message"]
    too_many = [{"parameter_index": 0, "value": 0.5}] * (
        script_module.MAX_BULK_PARAMETERS + 1)
    response = bulk_set_via_wire(inst, too_many)
    assert response["status"] == "error"
    assert "at most" in response["message"]
    # Nothing was scheduled and nothing was applied for any of these.
    assert inst.schedule_calls == []
    assert inst._song.tracks[0].devices[0].parameters[0].value == 0.05

    empty = bulk_set_via_wire(inst, [])
    assert empty["status"] == "success"
    assert empty["result"]["summary"] == {"total": 0, "succeeded": 0,
                                           "failed": 0, "skipped": 0}


def test_bulk_set_device_level_errors_are_per_item(script_module):
    inst = make_instance(script_module)
    response = inst._process_command({
        "type": "set_device_parameters",
        "params": {"track_index": 5, "device_index": 0,
                   "parameters": [{"parameter_index": 0, "value": 0.5},
                                  {"name": "Osc A Level", "value": 0.5}]}})
    assert response["status"] == "success"
    messages = [r["message"] for r in response["result"]["results"]]
    assert messages == ["Track index out of range"] * 2


def test_bulk_set_timing_opt_in(script_module):
    inst = make_instance(script_module)
    response = inst._process_command({
        "type": "set_device_parameters", "request_id": 7,
        "params": {"track_index": 0, "device_index": 0,
                   "parameters": [{"parameter_index": 0, "value": 0.5}]}})
    assert response["status"] == "success"
    assert response["request_id"] == 7
    assert "agent0_timing" in response


def test_new_command_advertised_at_bumped_version(script_module):
    assert "set_device_parameters" in script_module.SCRIPT_CAPABILITIES
    assert script_module.SCRIPT_VERSION == "1.7.10"
    info = script_module.AbletonMCP._get_script_info(
        make_instance(script_module))
    assert "set_device_parameters" in info["capabilities"]


# ------------------------------------------------------------ tool surface

SNIPPET = r"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.environ["REPO_ROOT"])

import MCP_Server.server as server

obs = {}
tools = asyncio.run(server.mcp.list_tools())
obs["schemas"] = {
    t.name: t.inputSchema for t in tools
    if t.name in ("get_device_parameters", "set_device_parameters")
}

calls = []


class FakeConn(object):
    def send_command(self, command_type, params=None, **kwargs):
        calls.append([command_type, params])
        return {"results": [], "summary": {"total": 0, "succeeded": 0,
                                           "failed": 0, "skipped": 0}}


server.get_ableton_connection = lambda: FakeConn()

server.get_device_parameters(None, 1, 2)
server.get_device_parameters(None, 1, 2, parameter_indices=[3, 4])
server.get_device_parameters(None, 1, 2, parameter_names=["Cutoff"])
server.set_device_parameters(
    None, 1, 2, [{"parameter_index": 3, "value": 0.5}])
server.set_device_parameters(
    None, 1, 2, [{"name": "Cutoff", "value": 0.5}], stop_on_error=True)
obs["calls"] = calls

print(json.dumps(obs))
"""


def test_tool_surface_and_wire_params():
    env = dict(os.environ)
    env["REPO_ROOT"] = str(REPO_ROOT)
    env["ABLETON_MCP_LEAN_PROFILE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-c", SNIPPET], env=env,
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    obs = json.loads(proc.stdout.strip().splitlines()[-1])

    read_props = obs["schemas"]["get_device_parameters"]["properties"]
    assert read_props["parameter_indices"]["default"] is None
    assert read_props["parameter_names"]["default"] is None
    assert "parameters" in obs["schemas"]["set_device_parameters"]["properties"]

    # A default read sends exactly the pre-MCP-06 params.
    assert obs["calls"][0] == [
        "get_device_parameters", {"track_index": 1, "device_index": 2}]
    assert obs["calls"][1] == [
        "get_device_parameters",
        {"track_index": 1, "device_index": 2, "parameter_indices": [3, 4]}]
    assert obs["calls"][2] == [
        "get_device_parameters",
        {"track_index": 1, "device_index": 2,
         "parameter_names": ["Cutoff"]}]
    assert obs["calls"][3] == [
        "set_device_parameters",
        {"track_index": 1, "device_index": 2,
         "parameters": [{"parameter_index": 3, "value": 0.5}]}]
    assert obs["calls"][4] == [
        "set_device_parameters",
        {"track_index": 1, "device_index": 2,
         "parameters": [{"name": "Cutoff", "value": 0.5}],
         "stop_on_error": True}]
