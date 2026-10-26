"""SI-245 tests: bool-by-truthiness family (SI-239 finding F6).

Boolean-ish params were read by truthiness, so the string "false"
-- the most likely wrong-type bool a JSON caller sends -- took the
TRUE branch everywhere: set_passive_listeners enabled="false"
enabled the listeners, execute_batch / set_device_parameters
stop_on_error="false" stopped on the first error, the
get_session_snapshot / get_track_info projection flags set to
"false" included the section, and timing="false" opted the request
into timing.

These pins cover the SI-245 validation at the dispatcher level
against a fake LOM, in the suite pattern (the SI-244 shape): every
probed bad shape refuses by name ("Invalid <field>: must be a
boolean", via the SI-244 _require_bool helper) with zero side
effects, and every valid control -- real bools, and every omitted
key -- behaves exactly as before. Validation sits where each value
is read: the timing opt-in in the _process_command head (refusal
envelope returned directly, like the request-id guard beside it),
enabled in _set_passive_listeners (the dispatcher forwards the raw
value), stop_on_error in _execute_batch / _set_device_parameters
(those methods' own early-guard style), and the include_* flags in
_get_session_snapshot / _get_track_info.

One deliberate exception in shape: include_automation's documented
default is None (no override; follows include_params), so an
explicit None keeps that meaning -- every other non-bool refuses.

The Remote Script normally runs inside Live, so _Framework is
stubbed and the class is instantiated without __init__ (the
bridge-test pattern). No Ableton, no network.
"""

import importlib.util
import sys
import threading
import types
from collections import deque
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLED_SCRIPT = (REPO_ROOT / "MCP_Server" / "bundled_ableton_remote_script"
                  / "AbletonMCP_init.py")

# The task-pinned wrong-type shapes, per site.
BAD_BOOLS = ["false", "true", 0, 1, []]


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
        "ableton_mcp_remote_script_si245", BUNDLED_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script_module():
    return _load_script_module()


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
    def __init__(self, volume=0.85):
        self.volume = FakeParam("Volume", volume)
        self.panning = FakeParam("Pan", 0.0)
        self.sends = [FakeParam("Send 0", 0.0)]


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
        self.mixer_device = FakeMixer(volume)
        self.clip_slots = list(slots)
        self.devices = list(devices)
        self.arrangement_clips = list(arrangement)


class FakeScene(object):
    def __init__(self, name):
        self.name = name
        self.tempo = 120.0
        self.is_triggered = False


class FakeSong(object):
    def __init__(self, tracks):
        self.tempo = 120.0
        self.signature_numerator = 4
        self.signature_denominator = 4
        self.tracks = list(tracks)
        self.return_tracks = []
        self.master_track = FakeTrack("Master", volume=0.9)
        self.master_track.mixer_device.sends = []
        self.scenes = [FakeScene("Scene 0")]
        self.cue_points = []
        self.is_playing = False
        self.current_song_time = 0.0
        self.song_length = 64.0
        self.loop = False
        self.loop_start = 0.0
        self.loop_length = 0.0


def make_instance(script_module):
    clip = FakeClip("Riff", notes=[FakeNote(60, 0.0)])
    device = FakeDevice("Device", [
        FakeParam("Param A", 0.5, automation_state=1),
        FakeParam("Param B", 0.25, automation_state=0),
    ])
    track = FakeTrack("Keys", devices=[device],
                      slots=[FakeClipSlot(clip)],
                      arrangement=[FakeClip("Arr")])
    inst = script_module.AbletonMCP.__new__(script_module.AbletonMCP)
    inst._song = FakeSong([track])
    inst.log_message = lambda *a, **k: None
    inst.show_message = lambda *a, **k: None
    inst.schedule_message = lambda delay, fn: fn()
    inst._passive_events = deque()
    inst._passive_lock = threading.Lock()
    inst._passive_max = script_module.PASSIVE_EVENT_QUEUE_MAX
    inst._passive_dropped = 0
    inst._passive_listeners_enabled = False
    inst._passive_track_count = None
    inst._passive_track_bindings = []
    inst._passive_clip_bindings = {}
    inst._song_passive_callbacks = []
    inst._dispatch_state = threading.local()
    return inst


def wire(inst, command_type, params=None, **extra):
    command = {"type": command_type, "params": params or {}}
    command.update(extra)
    return inst._process_command(command)


def assert_refused(response, field):
    assert response["status"] == "error", response
    assert response["message"] == (
        "Invalid %s: must be a boolean" % field), response


# ------------------------------------------------------------------- timing

def test_timing_wrong_types_refused_by_name(script_module):
    # Hunt T1: timing="false" carried agent0_timing at base.
    for value in BAD_BOOLS + [None, ""]:
        inst = make_instance(script_module)
        response = wire(inst, "get_session_info", timing=value)
        assert_refused(response, "timing")
        assert "agent0_timing" not in response


def test_timing_controls_unchanged(script_module):
    inst = make_instance(script_module)
    response = wire(inst, "get_session_info", timing=True)
    assert response["status"] == "success", response
    assert "agent0_timing" in response

    inst = make_instance(script_module)
    response = wire(inst, "get_session_info", timing=False)
    assert response["status"] == "success", response
    assert "agent0_timing" not in response

    inst = make_instance(script_module)
    response = wire(inst, "get_session_info")
    assert response["status"] == "success", response
    assert "agent0_timing" not in response


def test_timing_false_does_not_cancel_request_id_opt_in(script_module):
    # The request-id opt-in is independent: timing=False beside a
    # valid request_id still times, exactly as at base.
    inst = make_instance(script_module)
    response = wire(inst, "get_session_info",
                    timing=False, request_id=7)
    assert response["status"] == "success", response
    assert "agent0_timing" in response
    assert response["request_id"] == 7


# ------------------------------------------------------------------ enabled

def test_enabled_wrong_types_refused_by_name_no_side_effect(script_module):
    # Hunt L00/L04: enabled="false" and enabled=[1] enabled at base.
    for value in BAD_BOOLS + [None, "", [1]]:
        inst = make_instance(script_module)
        response = wire(inst, "set_passive_listeners",
                        {"enabled": value})
        assert_refused(response, "enabled")
        assert inst._passive_listeners_enabled is False, value


def test_enabled_controls_and_omitted_unchanged(script_module):
    inst = make_instance(script_module)
    response = wire(inst, "set_passive_listeners", {"enabled": True})
    assert response["status"] == "success", response
    assert response["result"] == {
        "enabled": True, "changed": True, "listener_count": 5}

    # Omitted "enabled" still just reports the current state.
    response = wire(inst, "set_passive_listeners", {})
    assert response["result"] == {
        "enabled": True, "changed": False, "listener_count": 5}

    response = wire(inst, "set_passive_listeners", {"enabled": False})
    assert response["result"] == {
        "enabled": False, "changed": True, "listener_count": 0}

    inst = make_instance(script_module)
    response = wire(inst, "set_passive_listeners", {})
    assert response["result"] == {
        "enabled": False, "changed": False, "listener_count": 0}


def test_set_passive_listeners_handler_itself_refuses(script_module):
    inst = make_instance(script_module)
    with pytest.raises(ValueError) as excinfo:
        inst._set_passive_listeners("false")
    assert str(excinfo.value) == "Invalid enabled: must be a boolean"


# ------------------------------------------------- execute_batch stop_on_error

BATCH_ITEMS = [
    {"type": "no_such_command", "params": {}},
    {"type": "get_track_info", "params": {"track_index": 0}},
]


def test_batch_stop_on_error_wrong_types_refused_by_name(script_module):
    # Hunt Q01: stop_on_error="false" skipped the second item.
    for value in BAD_BOOLS + [None]:
        inst = make_instance(script_module)
        response = wire(inst, "execute_batch", {
            "commands": BATCH_ITEMS, "stop_on_error": value})
        assert_refused(response, "stop_on_error")


def test_batch_stop_on_error_controls_unchanged(script_module):
    for params, expected in [
            ({}, ["error", "success"]),                 # omitted: False
            ({"stop_on_error": False}, ["error", "success"]),
            ({"stop_on_error": True}, ["error", "skipped"])]:
        inst = make_instance(script_module)
        payload = {"commands": BATCH_ITEMS}
        payload.update(params)
        response = wire(inst, "execute_batch", payload)
        assert response["status"] == "success", response
        assert [r["status"] for r in response["result"]["results"]] \
            == expected, params


# ------------------------------------------- set_device_parameters stop_on_error

BULK_ITEMS = [
    {"parameter_index": 99, "value": 0.5},
    {"parameter_index": 0, "value": 0.7},
]


def _bulk(inst, **extra):
    payload = {"track_index": 0, "device_index": 0,
               "parameters": BULK_ITEMS}
    payload.update(extra)
    return wire(inst, "set_device_parameters", payload)


def _param_a(inst):
    return inst._song.tracks[0].devices[0].parameters[0].value


def test_bulk_stop_on_error_wrong_types_refused_by_name(script_module):
    # Hunt B07: stop_on_error="false" skipped the good item.
    for value in BAD_BOOLS + [None]:
        inst = make_instance(script_module)
        response = _bulk(inst, stop_on_error=value)
        assert_refused(response, "stop_on_error")
        assert _param_a(inst) == 0.5, value  # nothing was applied


def test_bulk_stop_on_error_controls_unchanged(script_module):
    for extra, expected, applied in [
            ({}, ["error", "success"], 0.7),            # omitted: False
            ({"stop_on_error": False}, ["error", "success"], 0.7),
            ({"stop_on_error": True}, ["error", "skipped"], 0.5)]:
        inst = make_instance(script_module)
        response = _bulk(inst, **extra)
        assert response["status"] == "success", response
        assert [r["status"] for r in response["result"]["results"]] \
            == expected, extra
        assert _param_a(inst) == applied, extra


# ------------------------------------------------------- snapshot include_*

SNAPSHOT_FLAGS = [
    "include_notes", "include_params", "include_scenes",
    "include_return_tracks", "include_master_track",
    "include_cue_points", "include_clip_slots", "include_devices",
    "include_arrangement_clips", "include_automation",
]


def test_snapshot_flags_wrong_types_refused_by_name(script_module):
    # Hunt K01/K03: "false" still included / reported true at base.
    for key in SNAPSHOT_FLAGS:
        for value in BAD_BOOLS:
            inst = make_instance(script_module)
            response = wire(inst, "get_session_snapshot", {key: value})
            assert_refused(response, key)


def test_snapshot_flag_false_controls_omit_their_section(script_module):
    section_for = {
        "include_scenes": "scenes",
        "include_return_tracks": "return_tracks",
        "include_master_track": "master_track",
        "include_cue_points": "cue_points",
    }
    for key, section in section_for.items():
        inst = make_instance(script_module)
        response = wire(inst, "get_session_snapshot", {key: False})
        assert response["status"] == "success", response
        assert section not in response["result"], key

    per_track_for = {
        "include_clip_slots": "clip_slots",
        "include_devices": "devices",
        "include_arrangement_clips": "arrangement_clips",
    }
    for key, section in per_track_for.items():
        inst = make_instance(script_module)
        response = wire(inst, "get_session_snapshot", {key: False})
        assert section not in response["result"]["tracks"][0], key

    inst = make_instance(script_module)
    response = wire(inst, "get_session_snapshot",
                    {"include_notes": False, "include_params": False})
    result = response["result"]
    assert result["include_notes"] is False
    assert result["include_params"] is False
    clip = result["tracks"][0]["clip_slots"][0]["clip"]
    assert "notes" not in clip
    device = result["tracks"][0]["devices"][0]
    assert "parameters" not in device


def test_snapshot_omitted_flags_unchanged(script_module):
    inst = make_instance(script_module)
    response = wire(inst, "get_session_snapshot", {})
    assert response["status"] == "success", response
    result = response["result"]
    for section in ("scenes", "return_tracks", "master_track",
                    "cue_points"):
        assert section in result, section
    track = result["tracks"][0]
    for section in ("clip_slots", "devices", "arrangement_clips"):
        assert section in track, section
    assert result["include_notes"] is True
    assert result["include_params"] is True


def test_snapshot_include_automation_none_is_the_documented_sentinel(
        script_module):
    # include_automation defaults to None -- "no override, follow
    # include_params" -- and the MCP tool types it Optional[bool].
    # An explicit None keeps exactly the omitted behaviour; every
    # other non-bool refuses (pinned above).
    inst = make_instance(script_module)
    explicit = wire(inst, "get_session_snapshot",
                    {"include_automation": None})
    inst = make_instance(script_module)
    omitted = wire(inst, "get_session_snapshot", {})
    assert explicit["status"] == "success", explicit
    assert explicit["result"] == omitted["result"]


def test_snapshot_include_automation_bool_controls_unchanged(
        script_module):
    inst = make_instance(script_module)
    response = wire(inst, "get_session_snapshot",
                    {"include_automation": False})
    device = response["result"]["tracks"][0]["devices"][0]
    assert "automated_parameters" not in device
    assert "automation_enabled" not in device
    assert "parameters" in device  # include_params still on

    inst = make_instance(script_module)
    response = wire(inst, "get_session_snapshot", {
        "include_params": False, "include_automation": True})
    device = response["result"]["tracks"][0]["devices"][0]
    assert device["automated_parameters"] == ["Param A"]
    assert "parameters" not in device


def test_snapshot_handler_itself_refuses_wrong_types(script_module):
    inst = make_instance(script_module)
    with pytest.raises(ValueError) as excinfo:
        inst._get_session_snapshot(include_scenes="false")
    assert str(excinfo.value) == (
        "Invalid include_scenes: must be a boolean")


# ----------------------------------------------------- track-info include_*

def test_track_info_flags_wrong_types_refused_by_name(script_module):
    # Hunt K08/K10: "false" still included the section at base.
    for key in ("include_clip_slots", "include_devices"):
        for value in BAD_BOOLS + [None]:
            inst = make_instance(script_module)
            response = wire(inst, "get_track_info",
                            {"track_index": 0, key: value})
            assert_refused(response, key)


def test_track_info_flag_controls_unchanged(script_module):
    inst = make_instance(script_module)
    response = wire(inst, "get_track_info", {"track_index": 0})
    assert "clip_slots" in response["result"]
    assert "devices" in response["result"]

    inst = make_instance(script_module)
    response = wire(inst, "get_track_info", {
        "track_index": 0, "include_clip_slots": False})
    assert "clip_slots" not in response["result"]
    assert "devices" in response["result"]

    inst = make_instance(script_module)
    response = wire(inst, "get_track_info", {
        "track_index": 0, "include_devices": False})
    assert "devices" not in response["result"]
    assert "clip_slots" in response["result"]

    inst = make_instance(script_module)
    response = wire(inst, "get_track_info", {
        "track_index": 0, "include_clip_slots": True,
        "include_devices": True})
    assert "clip_slots" in response["result"]
    assert "devices" in response["result"]


def test_track_info_handler_itself_refuses_wrong_types(script_module):
    inst = make_instance(script_module)
    with pytest.raises(ValueError) as excinfo:
        inst._get_track_info(0, include_devices="false")
    assert str(excinfo.value) == (
        "Invalid include_devices: must be a boolean")
