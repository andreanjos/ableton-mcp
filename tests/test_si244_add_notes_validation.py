"""SI-244 tests: add_notes_to_clip notes validation.

SI-239 finding F5: _add_notes_to_clip assumed `notes` is a list of
dicts. A non-list notes (string, dict, int, None) and non-dict
entries failed with raw interpreter messages ('str' object has no
attribute 'get', 'int' object is not iterable, ...), and note
fields were forwarded unvalidated into the tuple handed to
Clip.set_notes - pitch "A", a NaN pitch, an infinite start_time
and a list velocity all reported success with the bad tuple
delivered (side-effect proven in the hunt).

These pins cover the SI-244 validation at the dispatcher level
against a fake LOM, in the suite pattern: every probed bad shape
refuses by name with zero side effects (Clip.set_notes is never
called), and every valid control behaves exactly as before -
including the notes=[{}] defaults tuple, byte-identical in value
AND type (control N12 in the hunt report).

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
        "ableton_mcp_remote_script_si244", BUNDLED_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script_module():
    return _load_script_module()


# ---------------------------------------------------------------- fake LOM

class FakeClip(object):
    def __init__(self, name="Clip"):
        self.name = name
        self.set_notes_calls = []

    def set_notes(self, notes_tuple):
        self.set_notes_calls.append(notes_tuple)


class FakeClipSlot(object):
    def __init__(self, clip=None):
        self.has_clip = clip is not None
        self.clip = clip


class FakeTrack(object):
    def __init__(self, name, slots=()):
        self.name = name
        self.clip_slots = list(slots)


class FakeSong(object):
    def __init__(self, tracks):
        self.tracks = list(tracks)


def make_instance(script_module):
    clip = FakeClip()
    track = FakeTrack("T0", slots=[FakeClipSlot(clip), FakeClipSlot()])
    song = FakeSong([track])
    inst = script_module.AbletonMCP.__new__(script_module.AbletonMCP)
    inst._song = song
    inst.log_message = lambda *a, **k: None
    inst.show_message = lambda *a, **k: None
    inst.schedule_message = lambda delay, fn: fn()
    return inst, clip


def wire(inst, **params):
    return inst._process_command(
        {"type": "add_notes_to_clip", "params": params})


def add(inst, notes):
    return wire(inst, track_index=0, clip_index=0, notes=notes)


def assert_refused(response, message):
    assert response["status"] == "error", response
    assert response["message"] == message, response


GOOD = {"pitch": 60, "start_time": 0.0, "duration": 0.25,
        "velocity": 100, "mute": False}
GOOD_TUPLE = (60, 0.0, 0.25, 100, False)


# ------------------------------------------------------------- container

def test_notes_container_shapes_refused_by_name(script_module):
    # Hunt N00/N01/N02/N06: each died raw at base ('str' object has
    # no attribute 'get', 'int' object is not iterable, ...). The
    # container rule mirrors the SI-241 selector rule, via the same
    # helper: a list or tuple is required.
    for value in ["abc", 123, {"pitch": 60}, None, 4.5, True]:
        inst, clip = make_instance(script_module)
        response = add(inst, value)
        assert_refused(response, "Invalid notes: must be a list or tuple")
        assert clip.set_notes_calls == [], value


def test_notes_tuple_container_accepted(script_module):
    # The SI-241 list-or-tuple rule, applied to the container: a
    # tuple of note dicts converts exactly like the list form.
    inst, clip = make_instance(script_module)
    response = add(inst, (dict(GOOD),))
    assert response["status"] == "success", response
    assert response["result"] == {"note_count": 1}
    assert clip.set_notes_calls == [(GOOD_TUPLE,)]


# --------------------------------------------------------------- entries

def test_note_entry_shapes_refused_by_name(script_module):
    # Hunt N03-N05: non-dict entries died raw ('...' object has no
    # attribute 'get'). The refusal voice mirrors the MCP-06 entry
    # refusal ("Invalid parameter_names: entries must be strings").
    for value in [[123], ["x"], [None], [[60]], [True], [("pitch", 60)]]:
        inst, clip = make_instance(script_module)
        response = add(inst, value)
        assert_refused(response, "Invalid notes: entries must be objects")
        assert clip.set_notes_calls == [], value


def test_bad_entry_after_good_one_delivers_nothing(script_module):
    # The whole list is converted before set_notes is called, so a
    # bad second entry means the good first note is NOT delivered:
    # validation is atomic, exactly as the base loop's
    # build-then-call shape allowed.
    inst, clip = make_instance(script_module)
    response = add(inst, [dict(GOOD), 123])
    assert_refused(response, "Invalid notes: entries must be objects")
    assert clip.set_notes_calls == []


# ---------------------------------------------------------------- fields

def test_bad_pitch_refused_by_name(script_module):
    # Hunt N10/N11: pitch "A" and a NaN pitch reported success with
    # the bad value delivered. Numeric strings are NOT coerced and
    # bools are not numbers (the SI-241/SI-243 rulings).
    bad = ["A", "60", NAN, INF, NINF, [60], {"p": 60}, None, True]
    for value in bad:
        inst, clip = make_instance(script_module)
        response = add(inst, [dict(GOOD, pitch=value)])
        assert_refused(response, "Invalid pitch: " + FINITE)
        assert clip.set_notes_calls == [], value


def test_bad_start_time_refused_by_name(script_module):
    # Hunt N11: an infinite start_time reported success, delivered.
    bad = [INF, NINF, NAN, "0.0", [0.0], None, False]
    for value in bad:
        inst, clip = make_instance(script_module)
        response = add(inst, [dict(GOOD, start_time=value)])
        assert_refused(response, "Invalid start_time: " + FINITE)
        assert clip.set_notes_calls == [], value


def test_bad_duration_refused_by_name(script_module):
    bad = [NAN, INF, NINF, "0.25", [0.25], None, True]
    for value in bad:
        inst, clip = make_instance(script_module)
        response = add(inst, [dict(GOOD, duration=value)])
        assert_refused(response, "Invalid duration: " + FINITE)
        assert clip.set_notes_calls == [], value


def test_bad_velocity_refused_by_name(script_module):
    # Hunt N11: a list velocity reported success, delivered.
    bad = [[100], NAN, INF, "100", {"v": 100}, None, True]
    for value in bad:
        inst, clip = make_instance(script_module)
        response = add(inst, [dict(GOOD, velocity=value)])
        assert_refused(response, "Invalid velocity: " + FINITE)
        assert clip.set_notes_calls == [], value


def test_bad_mute_refused_by_name(script_module):
    # Mute is a bool field: by truthiness the string "false" would
    # have been delivered as a truthy mute. Only real bools pass.
    bad = ["false", "true", 1, 0, None, [], [False]]
    for value in bad:
        inst, clip = make_instance(script_module)
        response = add(inst, [dict(GOOD, mute=value)])
        assert_refused(response, "Invalid mute: must be a boolean")
        assert clip.set_notes_calls == [], value


def test_hunt_combined_bad_note_refuses_on_first_bad_field(script_module):
    # Hunt N11 verbatim: pitch NaN, start_time Inf, duration 1,
    # velocity [100] reported success at base with the tuple
    # ((nan, inf, 1, [100], False),) delivered. Fields validate in
    # tuple order, so the pitch refusal is the one that surfaces.
    inst, clip = make_instance(script_module)
    response = add(inst, [{"pitch": NAN, "start_time": INF,
                           "duration": 1, "velocity": [100]}])
    assert_refused(response, "Invalid pitch: " + FINITE)
    assert clip.set_notes_calls == []


def test_explicit_none_field_is_not_an_absent_field(script_module):
    # A field present with value None refuses; only a genuinely
    # absent field takes its default (the .get defaults below).
    inst, clip = make_instance(script_module)
    response = add(inst, [{"pitch": None}])
    assert_refused(response, "Invalid pitch: " + FINITE)
    assert clip.set_notes_calls == []


# -------------------------------------------------------------- controls

def test_empty_dict_note_keeps_documented_defaults_byte_identical(
        script_module):
    # Hunt control N12: notes=[{}] succeeds with the documented
    # defaults. The delivered tuple is identical in value AND type
    # to base - validation does not coerce an int pitch/velocity
    # into floats.
    inst, clip = make_instance(script_module)
    response = add(inst, [{}])
    assert response["status"] == "success", response
    assert response["result"] == {"note_count": 1}
    assert clip.set_notes_calls == [((60, 0.0, 0.25, 100, False),)]
    delivered = clip.set_notes_calls[0][0]
    assert [type(v) for v in delivered] == [int, float, float, int, bool]


def test_well_formed_notes_forwarded_verbatim(script_module):
    inst, clip = make_instance(script_module)
    notes = [dict(GOOD),
             {"pitch": 62, "start_time": 1, "duration": 2,
              "velocity": 90, "mute": True}]
    response = add(inst, notes)
    assert response["status"] == "success", response
    assert response["result"] == {"note_count": 2}
    # Int fields stay ints, in order, exactly as base forwarded
    # them; no range rule is invented (finite is the only rule).
    assert clip.set_notes_calls == [
        (GOOD_TUPLE, (62, 1, 2, 90, True))]
    delivered = clip.set_notes_calls[0][1]
    assert [type(v) for v in delivered] == [int, int, int, int, bool]


def test_partial_note_takes_defaults_for_absent_fields(script_module):
    inst, clip = make_instance(script_module)
    response = add(inst, [{"pitch": 72}])
    assert response["status"] == "success", response
    assert clip.set_notes_calls == [((72, 0.0, 0.25, 100, False),)]


def test_extra_keys_still_ignored(script_module):
    # get_clip_notes returns note dicts carrying note_id; the
    # read -> add round-trip (pinned in test_clip_notes.py) depends
    # on unknown keys being ignored, exactly as at base.
    inst, clip = make_instance(script_module)
    response = add(inst, [dict(GOOD, note_id=7, anything="x")])
    assert response["status"] == "success", response
    assert clip.set_notes_calls == [(GOOD_TUPLE,)]


def test_empty_and_omitted_notes_unchanged(script_module):
    inst, clip = make_instance(script_module)
    response = add(inst, [])
    assert response["status"] == "success", response
    assert response["result"] == {"note_count": 0}
    assert clip.set_notes_calls == [()]

    inst, clip = make_instance(script_module)
    response = wire(inst, track_index=0, clip_index=0)
    assert response["status"] == "success", response
    assert response["result"] == {"note_count": 0}
    assert clip.set_notes_calls == [()]


def test_no_clip_refusal_keeps_precedence_over_notes(script_module):
    # Validation sits after the clip fetch (as the conversion loop
    # did at base), so an empty slot still refuses with the
    # pre-existing message even when the notes are also malformed.
    inst, _clip = make_instance(script_module)
    response = wire(inst, track_index=0, clip_index=1, notes="abc")
    assert_refused(response, "No clip in slot")
