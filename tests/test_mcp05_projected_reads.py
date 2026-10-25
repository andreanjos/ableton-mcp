"""MCP-05 tests: projected state reads (get_session_snapshot / get_track_info).

Pins:
* v1 invariance -- an unprojected call returns exactly the
  pre-projection result, pinned here against a hand-computed golden
  dict built field-for-field from the serializers (this test would
  pass against the pre-MCP-05 code unchanged);
* projection correctness -- a projected result is exactly the
  corresponding slice of the full result, with omitted sections
  absent (not emptied) and never serialised;
* the automated-parameter scan -- proven by call counts on a wrapped
  _automated_params_for_device, not by reading code: the scan runs
  when parameters are requested and is never invoked when they are
  not, with include_automation overriding either way;
* the wire path -- the dispatcher forwards projection params only
  when the caller sent them, and the MCP tools forward only
  non-default projection values, so a default call sends exactly the
  pre-projection params.

The Remote Script normally runs inside Live, so _Framework is stubbed
and the class is instantiated without __init__ (the bridge-test
pattern). No Ableton, no network.
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
REPO_SCRIPT = REPO_ROOT / "AbletonMCP_Remote_Script" / "__init__.py"
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
        "ableton_mcp_remote_script_mcp05", BUNDLED_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script_module():
    return _load_script_module()


def make_instance(script_module, song):
    inst = script_module.AbletonMCP.__new__(script_module.AbletonMCP)
    inst._song = song
    inst.log_message = lambda *a, **k: None
    inst.show_message = lambda *a, **k: None
    return inst


# ---------------------------------------------------------------- fake LOM

class FakeParam(object):
    def __init__(self, name, value=0.5, automation_state=0):
        self.name = name
        self.value = value
        self.min = 0.0
        self.max = 1.0
        self.is_enabled = True
        self.is_quantized = False
        self.value_string = "%.2f" % value
        self.automation_state = automation_state


class FakeDevice(object):
    def __init__(self, name, params):
        self.name = name
        self.class_name = "Operator"
        self.class_display_name = "Instrument"
        self.can_have_chains = False
        self.can_have_drum_pads = False
        self.parameters = params


class FakeNote(object):
    def __init__(self, pitch, start_time, velocity=100):
        self.pitch = pitch
        self.start_time = start_time
        self.duration = 0.25
        self.velocity = velocity
        self.mute = False


class FakeClip(object):
    def __init__(self, name, notes=()):
        self.name = name
        self.length = 16.0
        self.is_playing = False
        self.is_recording = False
        self.is_midi_clip = True
        self.is_audio_clip = False
        self.color = 0
        self.looping = True
        self.loop_start = 0.0
        self.loop_end = 16.0
        self.warping = False
        self.warp_mode = 0
        self.gain = 1.0
        self.pitch_coarse = 0
        self.pitch_fine = 0
        self.launch_mode = 0
        self.start_time = 0.0
        self.end_time = 16.0
        self._notes = list(notes)

    def get_notes_extended(self, _fp, _tp, _start, _dur):
        return list(self._notes)


class FakeClipSlot(object):
    def __init__(self, clip=None):
        self.has_clip = clip is not None
        self.clip = clip


class FakeMixer(object):
    def __init__(self, volume=0.85, sends=()):
        self.volume = FakeParam("Volume", volume)
        self.panning = FakeParam("Pan", 0.0)
        self.sends = list(sends)


class FakeTrack(object):
    def __init__(self, name, devices=(), slots=(), arrangement=(),
                 volume=0.85):
        self.name = name
        self.has_audio_input = False
        self.has_midi_input = True
        self.mute = False
        self.solo = False
        self.can_be_armed = True
        self.arm = False
        self.mixer_device = FakeMixer(
            volume, sends=[FakeParam("Send 0", 0.0)])
        self.clip_slots = list(slots)
        self.devices = list(devices)
        self.arrangement_clips = list(arrangement)


class FakeScene(object):
    def __init__(self, name):
        self.name = name
        self.tempo = 120.0
        self.is_triggered = False


class FakeSong(object):
    def __init__(self, tracks, return_tracks=(), master_devices=(),
                 scenes=(), cue_points=()):
        self.tempo = 120.0
        self.signature_numerator = 4
        self.signature_denominator = 4
        self.tracks = list(tracks)
        self.return_tracks = list(return_tracks)
        self.master_track = FakeTrack("Master", devices=master_devices,
                                      volume=0.9)
        self.master_track.mixer_device.sends = []
        self.scenes = list(scenes)
        self.cue_points = list(cue_points)
        self.is_playing = False
        self.current_song_time = 0.0
        self.song_length = 64.0
        self.loop = False
        self.loop_start = 0.0
        self.loop_length = 0.0


def golden_song():
    """The one-track song the golden dicts below are computed from."""
    clip = FakeClip("Riff", notes=[FakeNote(60, 0.0), FakeNote(64, 0.5, 90)])
    device = FakeDevice("Device", [
        FakeParam("Param A", 0.5, automation_state=1),
        FakeParam("Param B", 0.25, automation_state=0),
    ])
    track = FakeTrack("Keys", devices=[device],
                      slots=[FakeClipSlot(clip)])
    return FakeSong([track], scenes=[FakeScene("Scene 0")])


def multi_song():
    """Three tracks x two devices, one return, a master device."""
    def dev(name):
        return FakeDevice(name, [FakeParam("P0", 0.5, automation_state=1),
                                 FakeParam("P1", 0.25)])
    tracks = []
    for i in range(3):
        clip = FakeClip("Clip %d" % i, notes=[FakeNote(60, 0.0)])
        tracks.append(FakeTrack(
            "Track %d" % i,
            devices=[dev("D%d-a" % i), dev("D%d-b" % i)],
            slots=[FakeClipSlot(clip), FakeClipSlot()],
            arrangement=[FakeClip("Arr %d" % i)]))
    ret = FakeTrack("Return 0", devices=[dev("R0")])
    return FakeSong(tracks, return_tracks=[ret],
                    master_devices=[dev("M0")],
                    scenes=[FakeScene("Scene 0"), FakeScene("Scene 1")])


MULTI_DEVICE_TOTAL = 3 * 2 + 1 + 1  # tracks + return + master

# ------------------------------------------------------------- golden pins

GOLDEN_CLIP = {
    "name": "Riff", "length": 16.0, "is_playing": False,
    "is_recording": False, "is_midi_clip": True, "is_audio_clip": False,
    "color": 0, "looping": True, "loop_start": 0.0, "loop_end": 16.0,
    "warping": False, "warp_mode": 0, "gain": 1.0, "pitch_coarse": 0,
    "pitch_fine": 0, "launch_mode": 0,
    "notes": [
        {"pitch": 60, "start_time": 0.0, "duration": 0.25,
         "velocity": 100.0, "mute": False},
        {"pitch": 64, "start_time": 0.5, "duration": 0.25,
         "velocity": 90.0, "mute": False},
    ],
    "note_count": 2,
}

GOLDEN_DEVICE = {
    "index": 0, "name": "Device", "class_name": "Operator",
    "type": "instrument",
    "automated_parameters": ["Param A"], "automation_enabled": True,
    "parameters": [
        {"index": 0, "name": "Param A", "value": 0.5, "min": 0.0,
         "max": 1.0, "is_enabled": True, "is_quantized": False,
         "value_string": "0.50", "automation_state": 1},
        {"index": 1, "name": "Param B", "value": 0.25, "min": 0.0,
         "max": 1.0, "is_enabled": True, "is_quantized": False,
         "value_string": "0.25", "automation_state": 0},
    ],
}

GOLDEN_SNAPSHOT = {
    "schema": "ableton_mcp_snapshot_v2",
    "session": {
        "tempo": 120.0, "signature_numerator": 4,
        "signature_denominator": 4, "track_count": 1,
        "return_track_count": 0,
        "master_track": {"name": "Master", "volume": 0.9, "panning": 0.0},
        "is_playing": False, "current_song_time": 0.0,
        "song_length": 64.0, "loop": False, "loop_start": 0.0,
        "loop_length": 0.0,
    },
    "tracks": [{
        "index": 0, "name": "Keys", "is_audio_track": False,
        "is_midi_track": True, "mute": False, "solo": False, "arm": False,
        "volume": 0.85, "panning": 0.0,
        "sends": [{"index": 0, "value": 0.0, "name": "Send 0"}],
        "clip_slots": [{"index": 0, "has_clip": True, "clip": GOLDEN_CLIP}],
        "devices": [GOLDEN_DEVICE],
        "arrangement_clips": [],
    }],
    "scenes": [{"index": 0, "name": "Scene 0", "tempo": 120.0,
                "is_triggered": False}],
    "return_tracks": [],
    "master_track": {"volume": 0.9, "panning": 0.0, "devices": []},
    "cue_points": [],
    "include_notes": True, "include_params": True,
}

GOLDEN_TRACK_INFO = {
    "index": 0, "name": "Keys", "is_audio_track": False,
    "is_midi_track": True, "mute": False, "solo": False, "arm": False,
    "volume": 0.85, "panning": 0.0,
    "clip_slots": [{"index": 0, "has_clip": True, "clip": {
        "name": "Riff", "length": 16.0, "is_playing": False,
        "is_recording": False}}],
    "devices": [{"index": 0, "name": "Device", "class_name": "Operator",
                 "type": "instrument"}],
}


def test_both_script_copies_are_identical():
    assert REPO_SCRIPT.read_bytes() == BUNDLED_SCRIPT.read_bytes()


def test_unprojected_snapshot_matches_golden(script_module):
    inst = make_instance(script_module, golden_song())
    assert inst._get_session_snapshot() == GOLDEN_SNAPSHOT
    assert inst._get_session_snapshot(
        include_notes=True, include_params=True) == GOLDEN_SNAPSHOT


def test_unprojected_track_info_matches_golden(script_module):
    inst = make_instance(script_module, golden_song())
    assert inst._get_track_info(0) == GOLDEN_TRACK_INFO


# ------------------------------------------------------- projection slices

def test_track_indices_projection_is_a_slice_of_full(script_module):
    inst = make_instance(script_module, multi_song())
    full = inst._get_session_snapshot()
    projected = inst._get_session_snapshot(track_indices=[2, 0])
    # song order preserved, each entry identical to the full result
    assert projected["tracks"] == [full["tracks"][0], full["tracks"][2]]
    for key in full:
        if key != "tracks":
            assert projected[key] == full[key]
    # invalid and duplicate entries are ignored
    messy = inst._get_session_snapshot(track_indices=[1, 99, "x", 1])
    assert messy["tracks"] == [full["tracks"][1]]
    # an empty selection is a valid, empty projection
    assert inst._get_session_snapshot(track_indices=[])["tracks"] == []


def test_track_indices_infinity_entries_are_skipped(script_module):
    # SI-240: int(+-inf) raises OverflowError, which the selector's
    # except (TypeError, ValueError) did not catch, so a +-Infinity
    # entry failed the whole snapshot read with "cannot convert
    # float infinity to integer" instead of being skipped like
    # every other unusable entry (SI-239 F1, sibling of SI-236).
    inst = make_instance(script_module, multi_song())
    assert inst._get_session_snapshot(
        track_indices=[float("inf")])["tracks"] == []
    assert inst._get_session_snapshot(
        track_indices=[float("-inf")])["tracks"] == []
    assert inst._get_session_snapshot(
        track_indices=[float("inf"), float("-inf")])["tracks"] == []


def test_track_indices_nan_entry_is_skipped_control(script_module):
    # Control: int(NaN) raises ValueError, already caught at base,
    # so a NaN entry was already skipped; this pin must pass both
    # before and after the SI-240 fix.
    inst = make_instance(script_module, multi_song())
    assert inst._get_session_snapshot(
        track_indices=[float("nan")])["tracks"] == []


def test_track_indices_mixed_valid_and_infinity_returns_valid(script_module):
    # One bad entry must not poison an otherwise valid list:
    # the valid selection is still returned, in song order.
    inst = make_instance(script_module, multi_song())
    full = inst._get_session_snapshot()
    mixed = inst._get_session_snapshot(track_indices=[1, float("inf")])
    assert mixed["tracks"] == [full["tracks"][1]]
    mixed2 = inst._get_session_snapshot(
        track_indices=[0, float("-inf"), 2])
    assert mixed2["tracks"] == [full["tracks"][0], full["tracks"][2]]


def test_track_indices_non_list_selector_refused_by_name(script_module):
    # SI-241: a non-list track_indices was silently misread - a
    # string was iterated character-wise ("01" selected tracks 0 and
    # 1), a dict iterated its keys, a set iterated its members - or
    # died raw ("'int' object is not iterable" for 123). A selector
    # that is not a list/tuple refuses by name (SI-239 F2).
    inst = make_instance(script_module, multi_song())
    for bad in ("01", {"0": 1}, 123, 1.5, True, {0, 1}):
        with pytest.raises(ValueError) as excinfo:
            inst._get_session_snapshot(track_indices=bad)
        assert str(excinfo.value) == \
            "Invalid track_indices: must be a list or tuple", bad


def test_track_indices_wrong_type_entries_are_skipped(script_module):
    # SI-241: entries that are not real ints never select - floats
    # are no longer truncated ([1.9, 0.9] / [0.9] selected tracks 1
    # and 0 / track 0), True/False no longer select tracks 1/0, and
    # digit strings are no longer coerced (["0", "1"] selected
    # tracks 0 and 1). Wrong-type entries are skipped, preserving
    # the established skip-unusable-entries contract ("x"/None/
    # [[0]]/NaN/+-Inf are skipped, pinned above and by SI-240).
    inst = make_instance(script_module, multi_song())
    assert inst._get_session_snapshot(
        track_indices=[1.9, 0.9])["tracks"] == []
    assert inst._get_session_snapshot(
        track_indices=[0.9])["tracks"] == []
    assert inst._get_session_snapshot(
        track_indices=[True])["tracks"] == []
    assert inst._get_session_snapshot(
        track_indices=[False])["tracks"] == []
    assert inst._get_session_snapshot(
        track_indices=["0", "1"])["tracks"] == []
    full = inst._get_session_snapshot()
    mixed = inst._get_session_snapshot(track_indices=[1, 0.9])
    assert mixed["tracks"] == [full["tracks"][1]]
    mixed2 = inst._get_session_snapshot(track_indices=[2, True])
    assert mixed2["tracks"] == [full["tracks"][2]]


def test_track_indices_tuple_selector_still_works(script_module):
    # Control: a tuple selector was valid before SI-241 and stays
    # valid - only non-list/tuple containers change.
    inst = make_instance(script_module, multi_song())
    full = inst._get_session_snapshot()
    projected = inst._get_session_snapshot(track_indices=(0, 2))
    assert projected["tracks"] == [full["tracks"][0], full["tracks"][2]]


def test_section_projection_omits_keys(script_module):
    inst = make_instance(script_module, multi_song())
    full = inst._get_session_snapshot()
    projected = inst._get_session_snapshot(
        include_scenes=False, include_return_tracks=False,
        include_master_track=False, include_cue_points=False)
    for key in ("scenes", "return_tracks", "master_track", "cue_points"):
        assert key not in projected
    for key in ("schema", "session", "tracks",
                "include_notes", "include_params"):
        assert projected[key] == full[key]

    per_track = inst._get_session_snapshot(
        include_clip_slots=False, include_devices=False,
        include_arrangement_clips=False)
    for entry, full_entry in zip(per_track["tracks"], full["tracks"]):
        for key in ("clip_slots", "devices", "arrangement_clips"):
            assert key not in entry
        for key in full_entry:
            if key not in ("clip_slots", "devices", "arrangement_clips"):
                assert entry[key] == full_entry[key]
    # sections outside the tracks are untouched by per-track flags
    assert per_track["return_tracks"] == full["return_tracks"]
    assert per_track["master_track"] == full["master_track"]


def test_track_info_projection(script_module):
    inst = make_instance(script_module, golden_song())
    no_slots = inst._get_track_info(0, include_clip_slots=False)
    assert "clip_slots" not in no_slots
    assert no_slots["devices"] == GOLDEN_TRACK_INFO["devices"]
    no_devices = inst._get_track_info(0, include_devices=False)
    assert "devices" not in no_devices
    assert no_devices["clip_slots"] == GOLDEN_TRACK_INFO["clip_slots"]
    bare = inst._get_track_info(
        0, include_clip_slots=False, include_devices=False)
    assert set(bare) == set(GOLDEN_TRACK_INFO) - {"clip_slots", "devices"}


# -------------------------------------------------------- automated scan

class ScanCounter(object):
    """Wraps the instance scan method and counts invocations."""

    def __init__(self, inst):
        self.calls = 0
        original = inst._automated_params_for_device

        def counting(device):
            self.calls += 1
            return original(device)

        inst._automated_params_for_device = counting


def _all_devices(snapshot):
    devices = []
    for track in snapshot["tracks"]:
        devices.extend(track["devices"])
    for track in snapshot["return_tracks"]:
        devices.extend(track["devices"])
    devices.extend(snapshot["master_track"]["devices"])
    return devices


def test_automated_scan_runs_by_default(script_module):
    inst = make_instance(script_module, multi_song())
    counter = ScanCounter(inst)
    snapshot = inst._get_session_snapshot()
    assert counter.calls == MULTI_DEVICE_TOTAL
    for device in _all_devices(snapshot):
        assert device["automated_parameters"] == ["P0"]
        assert device["automation_enabled"] is True
        assert "parameters" in device


def test_automated_scan_skipped_when_params_not_requested(script_module):
    inst = make_instance(script_module, multi_song())
    counter = ScanCounter(inst)
    snapshot = inst._get_session_snapshot(include_params=False)
    assert counter.calls == 0
    for device in _all_devices(snapshot):
        assert "parameters" not in device
        assert "automated_parameters" not in device
        assert "automation_enabled" not in device


def test_automated_scan_overrides(script_module):
    inst = make_instance(script_module, multi_song())
    counter = ScanCounter(inst)
    forced = inst._get_session_snapshot(
        include_params=False, include_automation=True)
    assert counter.calls == MULTI_DEVICE_TOTAL
    for device in _all_devices(forced):
        assert device["automated_parameters"] == ["P0"]
        assert "parameters" not in device

    inst2 = make_instance(script_module, multi_song())
    counter2 = ScanCounter(inst2)
    suppressed = inst2._get_session_snapshot(
        include_params=True, include_automation=False)
    assert counter2.calls == 0
    for device in _all_devices(suppressed):
        assert "parameters" in device
        assert "automated_parameters" not in device
        assert "automation_enabled" not in device


def test_automated_scan_gating_reaches_nested_chain_devices(script_module):
    inst = make_instance(script_module, multi_song())
    counter = ScanCounter(inst)

    class FakeChain(object):
        def __init__(self, device):
            self.name = "Chain"
            self.mute = False
            self.solo = False
            self.mixer_device = FakeMixer(0.8)
            self.devices = [device]

    rack = FakeDevice("Rack", [FakeParam("Macro", 0.5)])
    rack.can_have_chains = True
    rack.chains = [FakeChain(FakeDevice("Nested", [
        FakeParam("NP", 0.5, automation_state=1)]))]

    lean = inst._serialize_device(rack, 0, include_params=False)
    assert counter.calls == 0
    assert "parameters" not in lean
    nested = lean["chains"][0]["devices"][0]
    assert "automated_parameters" not in nested
    assert "parameters" not in nested

    full = inst._serialize_device(rack, 0, include_params=True)
    assert counter.calls == 2  # rack itself + the nested device
    nested = full["chains"][0]["devices"][0]
    assert nested["automated_parameters"] == ["NP"]


# ------------------------------------------------------------ wire path

def test_dispatcher_projection_and_invariance(script_module):
    inst = make_instance(script_module, multi_song())
    plain = inst._process_command(
        {"type": "get_session_snapshot", "params": {}})
    assert plain["status"] == "success"
    assert plain["result"] == inst._get_session_snapshot()
    assert set(plain) == {"status", "result"}

    projected = inst._process_command({
        "type": "get_session_snapshot",
        "params": {"track_indices": [1], "include_scenes": False},
    })
    assert projected["result"]["tracks"] == [plain["result"]["tracks"][1]]
    assert "scenes" not in projected["result"]

    info = inst._process_command(
        {"type": "get_track_info", "params": {"track_index": 0}})
    assert info["result"] == inst._get_track_info(0)
    lean_info = inst._process_command({
        "type": "get_track_info",
        "params": {"track_index": 0, "include_devices": False},
    })
    assert "devices" not in lean_info["result"]
    assert lean_info["result"]["clip_slots"] == info["result"]["clip_slots"]


def test_dispatcher_legacy_params_only(script_module):
    """Params exactly as a pre-MCP-05 client sends them (the dataset
    consumer shape) keep working unchanged."""
    inst = make_instance(script_module, multi_song())
    resp = inst._process_command({
        "type": "get_session_snapshot",
        "params": {"include_notes": True, "include_params": True},
    })
    assert resp["result"] == inst._get_session_snapshot(
        include_notes=True, include_params=True)


# ------------------------------------------------- MCP tool surface (lean)

SNIPPET = r"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.environ["REPO_ROOT"])

import MCP_Server.server as server
from MCP_Server import script_handshake

obs = {}

tools = asyncio.run(server.mcp.list_tools())
obs["schemas"] = {
    t.name: t.inputSchema for t in tools
    if t.name in ("get_session_snapshot", "get_track_info")
}

calls = []


class FakeConn(object):
    def send_command(self, command_type, params=None, **kwargs):
        calls.append([command_type, params])
        if command_type == "get_script_info":
            return {"name": "AbletonMCP", "script_version": "1.7.7",
                    "protocol_version": 1,
                    "capabilities": ["get_session_snapshot"]}
        return {"ok": True}


conn = FakeConn()
script_handshake.handshake(conn.send_command)
calls.clear()
server.get_ableton_connection = lambda: conn

server.get_session_snapshot(None)
server.get_session_snapshot(
    None, track_indices=[1, 2], include_scenes=False,
    include_automation=True)
server.get_track_info(None, 2)
server.get_track_info(None, 2, include_devices=False)
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

    snap = obs["schemas"]["get_session_snapshot"]["properties"]
    assert snap["include_notes"]["default"] is True
    assert snap["include_params"]["default"] is True
    assert snap["track_indices"]["default"] is None
    assert snap["include_scenes"]["default"] is True
    assert snap["include_return_tracks"]["default"] is True
    assert snap["include_master_track"]["default"] is True
    assert snap["include_cue_points"]["default"] is True
    assert snap["include_clip_slots"]["default"] is True
    assert snap["include_devices"]["default"] is True
    assert snap["include_arrangement_clips"]["default"] is True
    assert snap["include_automation"]["default"] is None

    info = obs["schemas"]["get_track_info"]["properties"]
    assert info["include_clip_slots"]["default"] is True
    assert info["include_devices"]["default"] is True

    # a default call sends exactly the pre-projection params
    assert obs["calls"][0] == [
        "get_session_snapshot",
        {"include_notes": True, "include_params": True}]
    assert obs["calls"][1] == [
        "get_session_snapshot",
        {"include_notes": True, "include_params": True,
         "track_indices": [1, 2], "include_scenes": False,
         "include_automation": True}]
    assert obs["calls"][2] == ["get_track_info", {"track_index": 2}]
    assert obs["calls"][3] == [
        "get_track_info", {"track_index": 2, "include_devices": False}]


# ------------------------------------------------- SI-242 index guards
# The handler family's copy-pasted range guards validated no types
# (SI-239 F3): a bool index silently selected element 0/1 (and was
# echoed back as "index": true), NaN / integral floats passed both
# range comparisons and died at indexing with a raw interpreter
# message, and string/list/None indices died at the comparison
# itself. _require_index now refuses every non-int by name
# ("Invalid <field>: must be an integer"); real ints keep the exact
# old behaviour, including the existing out-of-range messages.
# The pins below cover the handler-thread read class
# (get_track_info / get_clip_notes), one scheduled-group handler
# (set_track_name), and the create-track adaptation site, whose
# index is consumed by the LOM rather than a Python list and so is
# validated for type only (no range check; -1 still means "end").

_WRONG_TYPE_INDICES = [float("nan"), 1.0, "0", [0], None]


def _track_info_response(inst, track_index):
    return inst._process_command(
        {"type": "get_track_info", "params": {"track_index": track_index}})


def _clip_notes_response(inst, clip_index):
    return inst._process_command(
        {"type": "get_clip_notes",
         "params": {"track_index": 0, "clip_index": clip_index}})


def test_track_info_bool_track_index_refused_by_name(script_module):
    # At base, True returned track 1's info with "index": true in the
    # result and False returned track 0's; both are refused now.
    inst = make_instance(script_module, multi_song())
    for bad in (True, False):
        response = _track_info_response(inst, bad)
        assert response["status"] == "error", response
        assert response["message"] == (
            "Invalid track_index: must be an integer")


def test_track_info_wrong_type_track_indices_refused_by_name(
        script_module):
    inst = make_instance(script_module, multi_song())
    for bad in _WRONG_TYPE_INDICES:
        response = _track_info_response(inst, bad)
        assert response["status"] == "error", (bad, response)
        assert response["message"] == (
            "Invalid track_index: must be an integer")


def test_track_info_index_controls_unchanged(script_module):
    inst = make_instance(script_module, multi_song())
    ok = _track_info_response(inst, 1)
    assert ok["status"] == "success", ok
    # The echoed index is the caller's real int, exactly as before.
    assert ok["result"]["index"] == 1
    assert type(ok["result"]["index"]) is int
    assert ok["result"]["name"] == "Track 1"
    for out_of_range in (99, -1):
        response = _track_info_response(inst, out_of_range)
        assert response["status"] == "error", response
        assert response["message"] == "Track index out of range"


def test_clip_notes_bool_clip_index_refused_by_name(script_module):
    # At base, True addressed slot 1 (the empty slot: "No clip in
    # slot") and False read slot 0 echoing "clip_index": false.
    inst = make_instance(script_module, multi_song())
    for bad in (True, False):
        response = _clip_notes_response(inst, bad)
        assert response["status"] == "error", response
        assert response["message"] == (
            "Invalid clip_index: must be an integer")


def test_clip_notes_wrong_type_clip_indices_refused_by_name(
        script_module):
    inst = make_instance(script_module, multi_song())
    for bad in _WRONG_TYPE_INDICES:
        response = _clip_notes_response(inst, bad)
        assert response["status"] == "error", (bad, response)
        assert response["message"] == (
            "Invalid clip_index: must be an integer")


def test_clip_notes_index_controls_unchanged(script_module):
    inst = make_instance(script_module, multi_song())
    ok = _clip_notes_response(inst, 0)
    assert ok["status"] == "success", ok
    assert ok["result"]["clip_index"] == 0
    assert ok["result"]["note_count"] == 1
    # Slot 1 exists but is empty: the valid-int path is untouched.
    empty = _clip_notes_response(inst, 1)
    assert empty["status"] == "error", empty
    assert empty["message"] == "No clip in slot"
    for out_of_range in (99, -1):
        response = _clip_notes_response(inst, out_of_range)
        assert response["status"] == "error", response
        assert response["message"] == "Clip index out of range"


def _scheduled_instance(script_module, song):
    """make_instance plus the inline scheduler the "main"-class
    commands need (the pattern of the mcp01/mcp06 harnesses)."""
    inst = make_instance(script_module, song)
    inst.schedule_message = lambda delay, fn: fn()
    return inst


def _set_track_name_response(inst, track_index):
    return inst._process_command(
        {"type": "set_track_name",
         "params": {"track_index": track_index, "name": "Renamed"}})


def test_scheduled_set_track_name_bool_index_refused_by_name(
        script_module):
    # At base, track_index=True renamed track 1 and False track 0.
    song = multi_song()
    inst = _scheduled_instance(script_module, song)
    for bad in (True, False):
        response = _set_track_name_response(inst, bad)
        assert response["status"] == "error", response
        assert response["message"] == (
            "Invalid track_index: must be an integer")
    assert [t.name for t in song.tracks] == [
        "Track 0", "Track 1", "Track 2"]


def test_scheduled_set_track_name_wrong_type_indices_refused_by_name(
        script_module):
    song = multi_song()
    inst = _scheduled_instance(script_module, song)
    for bad in _WRONG_TYPE_INDICES:
        response = _set_track_name_response(inst, bad)
        assert response["status"] == "error", (bad, response)
        assert response["message"] == (
            "Invalid track_index: must be an integer")
    assert [t.name for t in song.tracks] == [
        "Track 0", "Track 1", "Track 2"]


def test_scheduled_set_track_name_index_controls_unchanged(
        script_module):
    song = multi_song()
    inst = _scheduled_instance(script_module, song)
    ok = _set_track_name_response(inst, 1)
    assert ok["status"] == "success", ok
    assert [t.name for t in song.tracks] == [
        "Track 0", "Renamed", "Track 2"]
    for out_of_range in (99, -1):
        response = _set_track_name_response(inst, out_of_range)
        assert response["status"] == "error", response
        assert response["message"] == "Track index out of range"


class _CreatableSong(FakeSong):
    """multi_song's song plus the LOM create calls, recorded."""

    def __init__(self, tracks):
        super().__init__(tracks)
        self.created = []

    def create_midi_track(self, index=-1):
        self.created.append(("midi", index))
        track = FakeTrack("NewMIDI")
        if index == -1:
            self.tracks.append(track)
        else:
            self.tracks.insert(index, track)

    def create_audio_track(self, index=-1):
        self.created.append(("audio", index))
        track = FakeTrack("NewAudio")
        if index == -1:
            self.tracks.append(track)
        else:
            self.tracks.insert(index, track)


def test_create_track_wrong_type_index_refused_before_lom(
        script_module):
    # The create index is consumed by Song.create_*_track (the LOM),
    # not a Python list, so SI-242 validates its type only - but the
    # refusal must land BEFORE the LOM call: at base the fake LOM had
    # already recorded the call when the bad value died inside it.
    song = _CreatableSong([FakeTrack("A"), FakeTrack("B")])
    inst = _scheduled_instance(script_module, song)
    for command in ("create_midi_track", "create_audio_track"):
        for bad in [True, False] + _WRONG_TYPE_INDICES:
            response = inst._process_command(
                {"type": command, "params": {"index": bad}})
            assert response["status"] == "error", (command, bad, response)
            assert response["message"] == (
                "Invalid index: must be an integer")
    assert song.created == []
    assert len(song.tracks) == 2


def test_create_midi_track_valid_indices_unchanged(script_module):
    song = _CreatableSong([FakeTrack("A"), FakeTrack("B")])
    inst = _scheduled_instance(script_module, song)
    at_end = inst._process_command(
        {"type": "create_midi_track", "params": {"index": -1}})
    assert at_end["status"] == "success", at_end
    assert at_end["result"]["index"] == 2
    at_zero = inst._process_command(
        {"type": "create_midi_track", "params": {"index": 0}})
    assert at_zero["status"] == "success", at_zero
    assert at_zero["result"]["index"] == 0
    assert song.created == [("midi", -1), ("midi", 0)]
