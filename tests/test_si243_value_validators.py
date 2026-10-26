"""SI-243 tests: value validation in the Remote Script handlers.

SI-239 finding F4: the value-taking handlers forwarded caller values
to the Live object model unchecked - set_tempo assigned NaN, "fast",
[120], None and True to Song.tempo verbatim; the float()-converted
time handlers accepted NaN/+/-Infinity and numeric strings and died
raw on None/list; create_clip forwarded its length verbatim; the
name setters assigned non-strings verbatim (create_locator silently
stringified them); create_audio_clip's path died raw inside
os.path.isabs for non-strings.

These pins cover the SI-243 validators (_require_finite_number /
_require_string) at the dispatcher level against a fake LOM, in the
suite pattern: every probed bad shape refuses by name with zero side
effects, and every valid control behaves exactly as before. The
set_device_parameter family is pinned in test_mcp06_bulk_params.py.

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

FINITE = "must be a finite number"
NAN = float("nan")
INF = float("inf")
NINF = float("-inf")


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
        "ableton_mcp_remote_script_si243", BUNDLED_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script_module():
    return _load_script_module()


# ---------------------------------------------------------------- fake LOM

class FakeParam(object):
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


class FakeClip(object):
    def __init__(self, name, length=4.0):
        self.name = name
        self.length = length
        self.is_midi_clip = True
        self.is_audio_clip = False


class FakeClipSlot(object):
    def __init__(self, clip=None):
        self.has_clip = clip is not None
        self.clip = clip
        self.created_length = "UNSET"
        self.created_audio_path = "UNSET"

    def create_clip(self, length):
        self.created_length = length
        self.clip = FakeClip("NewClip", length)
        self.has_clip = True

    def create_audio_clip(self, path):
        self.created_audio_path = path
        self.clip = FakeClip("AudioClip", 4.0)
        self.clip.is_audio_clip = True
        self.has_clip = True


class FakeDevice(object):
    def __init__(self, name, params):
        self.name = name
        self.class_name = "Operator"
        self.class_display_name = "Instrument"
        self.can_have_chains = False
        self.can_have_drum_pads = False
        self.parameters = params


class FakeTrack(object):
    def __init__(self, name, devices=(), slots=(), arrangement=()):
        self.name = name
        self.has_audio_input = True
        self.has_midi_input = False
        self.devices = list(devices)
        self.clip_slots = list(slots)
        self.arrangement_clips = list(arrangement)
        self.dup_calls = []

    def duplicate_clip_to_arrangement(self, clip, destination_time):
        self.dup_calls.append(destination_time)


class FakeCue(object):
    def __init__(self, time):
        self.time = time
        self.name = ""


class FakeSong(object):
    def __init__(self, tracks):
        self.tracks = list(tracks)
        self.tempo = 120.0
        self.current_song_time = 0.0
        self.cue_points = []

    def set_or_delete_cue(self):
        self.cue_points.append(FakeCue(self.current_song_time))


def make_instance(script_module):
    device = FakeDevice("Synth", [FakeParam("Filter Cutoff", 0.5),
                                  FakeParam("Resonance", 0.3)])
    track = FakeTrack(
        "T0", devices=[device],
        slots=[FakeClipSlot(FakeClip("Existing")), FakeClipSlot()],
        arrangement=[FakeClip("ArrClip")])
    song = FakeSong([track])
    inst = script_module.AbletonMCP.__new__(script_module.AbletonMCP)
    inst._song = song
    inst.log_message = lambda *a, **k: None
    inst.show_message = lambda *a, **k: None
    inst.schedule_message = lambda delay, fn: fn()
    return inst, song, track


def wire(inst, command_type, **params):
    return inst._process_command({"type": command_type, "params": params})


def assert_refused(response, message):
    assert response["status"] == "error", response
    assert response["message"] == message, response


# ---------------------------------------------------------------- set_tempo

def test_set_tempo_bad_values_refused_by_name(script_module):
    bad = [NAN, INF, NINF, "fast", "120", [120], {"x": 1}, None, True, False]
    for value in bad:
        inst, song, _track = make_instance(script_module)
        response = wire(inst, "set_tempo", tempo=value)
        assert_refused(response, "Invalid tempo: " + FINITE)
        assert song.tempo == 120.0, value


def test_set_tempo_valid_values_unchanged(script_module):
    inst, song, _track = make_instance(script_module)
    response = wire(inst, "set_tempo", tempo=128.5)
    assert response["status"] == "success", response
    assert song.tempo == 128.5
    # An int tempo lands as its float (the conversion every other
    # numeric handler already applied via float()).
    inst, song, _track = make_instance(script_module)
    response = wire(inst, "set_tempo", tempo=120)
    assert response["status"] == "success", response
    assert song.tempo == 120.0
    # Omitted tempo keeps the dispatcher's 120.0 default.
    inst, song, _track = make_instance(script_module)
    response = inst._process_command({"type": "set_tempo", "params": {}})
    assert response["status"] == "success", response
    assert song.tempo == 120.0


# ------------------------------------------------------- current song time

def test_set_current_song_time_bad_values_refused_by_name(script_module):
    bad = ["3.5", NAN, INF, NINF, True, None, [1.0], {"x": 1}]
    for value in bad:
        inst, song, _track = make_instance(script_module)
        response = wire(inst, "set_current_song_time", time=value)
        assert_refused(response, "Invalid time: " + FINITE)
        assert song.current_song_time == 0.0, value


def test_set_current_song_time_valid_unchanged(script_module):
    inst, song, _track = make_instance(script_module)
    response = wire(inst, "set_current_song_time", time=2.0)
    assert response["status"] == "success", response
    assert song.current_song_time == 2.0
    inst, song, _track = make_instance(script_module)
    response = wire(inst, "set_current_song_time", time=3)
    assert response["status"] == "success", response
    assert song.current_song_time == 3.0


# ------------------------------------------------- duplicate to arrangement

def test_duplicate_bad_destination_time_refused_by_name(script_module):
    bad = ["1.5", NAN, INF, NINF, True, None, [1.0]]
    for value in bad:
        inst, _song, track = make_instance(script_module)
        response = wire(inst, "duplicate_session_clip_to_arrangement",
                        track_index=0, clip_index=0, destination_time=value)
        assert_refused(response, "Invalid destination_time: " + FINITE)
        assert track.dup_calls == [], value


def test_duplicate_valid_destination_time_unchanged(script_module):
    inst, _song, track = make_instance(script_module)
    response = wire(inst, "duplicate_session_clip_to_arrangement",
                    track_index=0, clip_index=0, destination_time=4.0)
    assert response["status"] == "success", response
    assert track.dup_calls == [4.0]
    assert response["result"]["destination_time"] == 4.0
    # An int time reaches the LOM as its float (as float() produced
    # before) while the result keeps echoing the caller's own value.
    inst, _song, track = make_instance(script_module)
    response = wire(inst, "duplicate_session_clip_to_arrangement",
                    track_index=0, clip_index=0, destination_time=4)
    assert response["status"] == "success", response
    assert track.dup_calls == [4.0]
    assert response["result"]["destination_time"] == 4
    assert type(response["result"]["destination_time"]) is int


# ------------------------------------------------------------ create_locator

def test_create_locator_bad_time_refused_before_any_cue(script_module):
    # At base a NaN/Inf time actually created a cue at that time on
    # the song before the lookup failed; the refusal now lands before
    # any side effect.
    bad = ["2.0", NAN, INF, NINF, True, None, [2.0]]
    for value in bad:
        inst, song, _track = make_instance(script_module)
        response = wire(inst, "create_locator", name="L", time=value)
        assert_refused(response, "Invalid time: " + FINITE)
        assert song.cue_points == [], value
        assert song.current_song_time == 0.0, value


def test_create_locator_bad_name_refused_by_name(script_module):
    bad = [["x"], 123, None, {"x": 1}, True]
    for value in bad:
        inst, song, _track = make_instance(script_module)
        response = wire(inst, "create_locator", name=value, time=2.0)
        assert_refused(response, "Invalid name: must be a string")
        assert song.cue_points == [], value


def test_create_locator_valid_unchanged(script_module):
    inst, song, _track = make_instance(script_module)
    response = wire(inst, "create_locator", name="Loc", time=8.0)
    assert response["status"] == "success", response
    assert [(c.time, c.name) for c in song.cue_points] == [(8.0, "Loc")]
    # An empty name still creates an unnamed locator (the falsy-name
    # skip is unchanged).
    inst, song, _track = make_instance(script_module)
    response = wire(inst, "create_locator", name="", time=16.0)
    assert response["status"] == "success", response
    assert [(c.time, c.name) for c in song.cue_points] == [(16.0, "")]


# --------------------------------------------------------------- create_clip

def test_create_clip_bad_lengths_refused_by_name(script_module):
    bad = [NAN, INF, NINF, "4", [4], {"x": 1}, None, True]
    for value in bad:
        inst, _song, track = make_instance(script_module)
        slot = track.clip_slots[1]
        response = wire(inst, "create_clip", track_index=0, clip_index=1,
                        length=value)
        assert_refused(response, "Invalid length: " + FINITE)
        assert slot.created_length == "UNSET", value
        assert slot.has_clip is False, value


def test_create_clip_valid_lengths_forwarded_unchanged(script_module):
    # Finite lengths forward exactly as before - including negative
    # and zero lengths: no range rule is invented here; length
    # semantics stay Live's.
    for value, landed in [(4.0, 4.0), (-4.0, -4.0), (0.0, 0.0), (8, 8.0)]:
        inst, _song, track = make_instance(script_module)
        slot = track.clip_slots[1]
        response = wire(inst, "create_clip", track_index=0, clip_index=1,
                        length=value)
        assert response["status"] == "success", (value, response)
        assert slot.created_length == landed, value


# -------------------------------------------------------------- name setters

BAD_NAMES = [["a", "b"], None, 123, {"x": 1}, True]


def test_name_setters_refuse_non_strings_by_name(script_module):
    for value in BAD_NAMES:
        inst, _song, track = make_instance(script_module)
        response = wire(inst, "set_track_name", track_index=0, name=value)
        assert_refused(response, "Invalid name: must be a string")
        assert track.name == "T0", value

        inst, _song, track = make_instance(script_module)
        response = wire(inst, "set_clip_name", track_index=0, clip_index=0,
                        name=value)
        assert_refused(response, "Invalid name: must be a string")
        assert track.clip_slots[0].clip.name == "Existing", value

        inst, _song, track = make_instance(script_module)
        response = wire(inst, "set_arrangement_clip_name", track_index=0,
                        clip_index=0, name=value)
        assert_refused(response, "Invalid name: must be a string")
        assert track.arrangement_clips[0].name == "ArrClip", value


def test_name_setters_valid_names_unchanged(script_module):
    inst, _song, track = make_instance(script_module)
    response = wire(inst, "set_track_name", track_index=0, name="NewName")
    assert response["status"] == "success", response
    assert track.name == "NewName"
    # The empty string is a string: unchanged.
    inst, _song, track = make_instance(script_module)
    response = wire(inst, "set_track_name", track_index=0, name="")
    assert response["status"] == "success", response
    assert track.name == ""

    inst, _song, track = make_instance(script_module)
    response = wire(inst, "set_clip_name", track_index=0, clip_index=0,
                    name="ClipName")
    assert response["status"] == "success", response
    assert track.clip_slots[0].clip.name == "ClipName"

    inst, _song, track = make_instance(script_module)
    response = wire(inst, "set_arrangement_clip_name", track_index=0,
                    clip_index=0, name="ArrName")
    assert response["status"] == "success", response
    assert track.arrangement_clips[0].name == "ArrName"


# ---------------------------------------------------------- create_audio_clip

def test_create_audio_clip_path_shapes(script_module):
    # Truthy non-string paths died raw inside os.path.isabs at base;
    # they now refuse by name (a bytes path is a direct-caller-only
    # shape - not JSON-representable - and refuses like the rest).
    for value in [123, ["x"], {"x": 1}, 4.5, b"/tmp/x.wav"]:
        inst, _song, track = make_instance(script_module)
        slot = track.clip_slots[1]
        response = wire(inst, "create_audio_clip", track_index=0,
                        clip_index=1, path=value)
        assert_refused(response, "Invalid path: must be a string")
        assert slot.created_audio_path == "UNSET", value
    # The pre-existing falsy guard is byte-identical and still first.
    for value in [None, ""]:
        inst, _song, track = make_instance(script_module)
        response = wire(inst, "create_audio_clip", track_index=0,
                        clip_index=1, path=value)
        assert_refused(response, "Audio file path is required")
    # Relative-path refusal unchanged.
    inst, _song, track = make_instance(script_module)
    response = wire(inst, "create_audio_clip", track_index=0, clip_index=1,
                    path="relative.wav")
    assert_refused(response,
                   "Audio file path must be absolute (got: relative.wav)")
    # Valid absolute path unchanged.
    inst, _song, track = make_instance(script_module)
    slot = track.clip_slots[1]
    response = wire(inst, "create_audio_clip", track_index=0, clip_index=1,
                    path="/tmp/x.wav")
    assert response["status"] == "success", response
    assert slot.created_audio_path == "/tmp/x.wav"
