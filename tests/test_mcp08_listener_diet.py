"""MCP-08: the Remote Script listener/serializer diet.

Pins, against an instrumented fake LOM whose objects count every
listener registration:

* at startup (and in any session that never enables events) the
  script registers ZERO passive listeners - the census exists only
  after set_passive_listeners negotiates it on;
* enabling registers the full census exactly once (idempotent),
  disabling removes every listener, and the events that then flow
  have exactly the pre-MCP-08 shapes (hand-computed goldens);
* the clip-listener leak is fixed: listeners bound from the slot
  callback are tracked in a registry, so slot toggles, clip swaps,
  clip removal and track removal all leave a constant listener
  count and empty registries on the departed objects;
* high-frequency value events (volume / panning / tempo) coalesce
  latest-value-wins per source while discrete events never do;
* the event queue is a bounded deque: overflow drops the OLDEST
  events, the drop total is reported (and reset) by the next drain
  as "dropped", and appends stay O(1) - no whole-queue slicing;
* the MCP dataset poller - the in-repo consumer - enables the
  listeners while it runs and releases them when it stops; a
  pre-negotiation script (no set_passive_listeners capability) is
  left alone, since its listeners are always on;
* the server tool sends clean wire params and surfaces an old
  script's Unknown command as the standard Error string.
"""

import asyncio
import json
import os
import subprocess
import sys
import threading
import types
from collections import deque
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tests.test_remote_script_bridge import _load_script_module  # noqa: E402

script_module = _load_script_module()


# ── Instrumented fake LOM ──────────────────────────────────────────

class ListenerHost(object):
    """Live-style listener registry: add/remove/fire per kind, counted."""

    def __init__(self):
        self._listeners = {}

    def _add(self, kind, cb):
        self._listeners.setdefault(kind, []).append(cb)

    def _remove(self, kind, cb):
        lst = self._listeners.get(kind, [])
        if cb in lst:
            lst.remove(cb)

    def listener_count(self, kind=None):
        if kind is not None:
            return len(self._listeners.get(kind, []))
        return sum(len(v) for v in self._listeners.values())

    def fire(self, kind):
        for cb in list(self._listeners.get(kind, [])):
            cb()


def _listener_methods(kind):
    def add(self, cb):
        self._add(kind, cb)

    def remove(self, cb):
        self._remove(kind, cb)

    return add, remove


class FakeParam(ListenerHost):
    def __init__(self, value=0.5):
        super().__init__()
        self.value = value

    add_value_listener, remove_value_listener = _listener_methods("value")


class FakeClip(ListenerHost):
    def __init__(self, name):
        super().__init__()
        self.name = name
        self.is_playing = False

    add_name_listener, remove_name_listener = _listener_methods("name")
    add_notes_listener, remove_notes_listener = _listener_methods("notes")
    add_playing_status_listener, remove_playing_status_listener = \
        _listener_methods("playing_status")


class FakeClipSlot(ListenerHost):
    def __init__(self, clip=None):
        super().__init__()
        self.has_clip = clip is not None
        self.clip = clip

    add_has_clip_listener, remove_has_clip_listener = \
        _listener_methods("has_clip")


class FakeMixer(object):
    def __init__(self, volume=0.85, panning=0.0):
        self.volume = FakeParam(volume)
        self.panning = FakeParam(panning)


class FakeTrack(ListenerHost):
    def __init__(self, name, slots=()):
        super().__init__()
        self.name = name
        self.mute = False
        self.solo = False
        self.arm = False
        self.can_be_armed = True
        self.devices = []
        self.clip_slots = list(slots)
        self.mixer_device = FakeMixer()

    add_name_listener, remove_name_listener = _listener_methods("name")
    add_mute_listener, remove_mute_listener = _listener_methods("mute")
    add_solo_listener, remove_solo_listener = _listener_methods("solo")
    add_arm_listener, remove_arm_listener = _listener_methods("arm")
    add_devices_listener, remove_devices_listener = \
        _listener_methods("devices")


class FakeSong(ListenerHost):
    def __init__(self, tracks):
        super().__init__()
        self.tracks = list(tracks)
        self.tempo = 120.0
        self.signature_numerator = 4
        self.signature_denominator = 4
        self.is_playing = False

    add_tempo_listener, remove_tempo_listener = _listener_methods("tempo")
    add_signature_numerator_listener, remove_signature_numerator_listener = \
        _listener_methods("signature_numerator")
    add_signature_denominator_listener, \
        remove_signature_denominator_listener = \
        _listener_methods("signature_denominator")
    add_is_playing_listener, remove_is_playing_listener = \
        _listener_methods("is_playing")
    add_tracks_listener, remove_tracks_listener = _listener_methods("tracks")


def build_song():
    """2 tracks: A has slots [clip, empty], B has slots [clip]."""
    clip_a = FakeClip("Clip A")
    clip_b = FakeClip("Clip B")
    track_a = FakeTrack("A", slots=[FakeClipSlot(clip_a), FakeClipSlot()])
    track_b = FakeTrack("B", slots=[FakeClipSlot(clip_b)])
    return FakeSong([track_a, track_b]), clip_a, clip_b


# 5 song + per track (5 track + 2 mixer) + 1 per slot + 3 per clip
EXPECTED_CENSUS = 5 + (7 + 2) + (7 + 1) + 3 + 3  # = 28


def total_listeners(song):
    total = song.listener_count()
    for track in song.tracks:
        total += track.listener_count()
        total += track.mixer_device.volume.listener_count()
        total += track.mixer_device.panning.listener_count()
        for slot in track.clip_slots:
            total += slot.listener_count()
            if slot.clip is not None:
                total += slot.clip.listener_count()
    return total


def make_instance(song):
    """A script instance with __init__'s passive state, no socket."""
    inst = script_module.AbletonMCP.__new__(script_module.AbletonMCP)
    inst._song = song
    inst.log_message = lambda *a, **k: None
    inst.show_message = lambda *a, **k: None
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
    scheduled = []

    def schedule_message(_delay, fn):
        scheduled.append(fn)
        fn()

    inst.schedule_message = schedule_message
    inst.scheduled = scheduled
    return inst


def wire(inst, command_type, params=None):
    response = inst._process_command(
        {"type": command_type, "params": params or {}})
    assert response["status"] == "success", response
    return response["result"]


def enable(inst):
    return wire(inst, "set_passive_listeners", {"enabled": True})


def drain_events(inst):
    result = wire(inst, "drain_passive_events")
    assert set(result.keys()) == {"events", "count", "dropped"}
    assert result["count"] == len(result["events"])
    return result


def strip_ts(events):
    return [{k: v for k, v in e.items() if k != "ts"} for e in events]


# ── Negotiation: off by default, on only when asked ──────────────

def test_no_listeners_until_enabled():
    song, _a, _b = build_song()
    inst = make_instance(song)
    assert inst._passive_listeners_enabled is False
    assert inst._passive_listener_count() == 0
    assert total_listeners(song) == 0
    # Handshake: legacy field kept, negotiated marker added, live state.
    info = wire(inst, "get_script_info")
    assert info["passive_listeners"] is True
    assert info["agent0_passive_listeners_negotiated"] is True
    assert info["passive_listeners_enabled"] is False
    assert "set_passive_listeners" in info["capabilities"]
    # Firing a would-be source enqueues nothing: nobody is listening.
    song.fire("tempo")
    assert drain_events(inst)["events"] == []


def test_enable_registers_full_census_once():
    song, _a, _b = build_song()
    inst = make_instance(song)
    result = enable(inst)
    assert result == {"enabled": True, "changed": True,
                      "listener_count": EXPECTED_CENSUS}
    assert total_listeners(song) == EXPECTED_CENSUS
    # Song 5, per-track and per-clip distribution.
    assert song.listener_count() == 5
    track_a = song.tracks[0]
    assert track_a.listener_count() == 5
    assert track_a.mixer_device.volume.listener_count() == 1
    assert track_a.mixer_device.panning.listener_count() == 1
    assert track_a.clip_slots[0].listener_count() == 1
    assert track_a.clip_slots[0].clip.listener_count() == 3
    # Idempotent: a second enable registers nothing more.
    result = enable(inst)
    assert result == {"enabled": True, "changed": False,
                      "listener_count": EXPECTED_CENSUS}
    assert total_listeners(song) == EXPECTED_CENSUS
    info = wire(inst, "get_script_info")
    assert info["passive_listeners_enabled"] is True


def test_status_query_without_enabled_param():
    song, _a, _b = build_song()
    inst = make_instance(song)
    result = wire(inst, "set_passive_listeners")
    assert result == {"enabled": False, "changed": False,
                      "listener_count": 0}
    enable(inst)
    result = wire(inst, "set_passive_listeners")
    assert result == {"enabled": True, "changed": False,
                      "listener_count": EXPECTED_CENSUS}


def test_disable_removes_every_listener():
    song, clip_a, clip_b = build_song()
    inst = make_instance(song)
    enable(inst)
    result = wire(inst, "set_passive_listeners", {"enabled": False})
    assert result == {"enabled": False, "changed": True,
                      "listener_count": 0}
    assert total_listeners(song) == 0
    assert clip_a.listener_count() == 0
    assert clip_b.listener_count() == 0
    assert inst._passive_clip_bindings == {}
    assert inst._passive_track_bindings == []
    # Disabling again is a no-op.
    result = wire(inst, "set_passive_listeners", {"enabled": False})
    assert result["changed"] is False


def test_enable_inside_batch_runs_inline():
    song, _a, _b = build_song()
    inst = make_instance(song)
    response = inst._process_command({
        "type": "execute_batch",
        "params": {"commands": [
            {"type": "set_passive_listeners", "params": {"enabled": True}},
            {"type": "drain_passive_events", "params": {}},
        ]},
    })
    assert response["status"] == "success", response
    items = response["result"]["results"]
    assert items[0]["result"]["enabled"] is True
    assert items[1]["result"]["events"] == []
    # One scheduling for the whole batch (the batch's own).
    assert len(inst.scheduled) == 1
    assert total_listeners(song) == EXPECTED_CENSUS


# ── Default event flow: shapes unchanged when enabled ─────────────

def test_event_flow_golden():
    song, clip_a, _b = build_song()
    inst = make_instance(song)
    enable(inst)
    song.tempo = 128.5
    song.fire("tempo")
    song.is_playing = True
    song.fire("is_playing")
    track_a = song.tracks[0]
    track_a.name = "Renamed"
    track_a.fire("name")
    clip_a.name = "New Clip"
    clip_a.fire("name")
    track_a.mixer_device.volume.value = 0.42
    track_a.mixer_device.volume.fire("value")
    result = drain_events(inst)
    assert result["dropped"] == 0
    assert strip_ts(result["events"]) == [
        {"type": "tempo_changed", "track_index": None, "clip_index": None,
         "detail": {"tempo": 128.5}},
        {"type": "playback_changed", "track_index": None, "clip_index": None,
         "detail": {"is_playing": True}},
        {"type": "name_changed", "track_index": 0, "clip_index": None,
         "detail": {"name": "Renamed"}},
        {"type": "clip_name_changed", "track_index": 0, "clip_index": 0,
         "detail": {"name": "New Clip"}},
        {"type": "volume_changed", "track_index": 0, "clip_index": None,
         "detail": {"volume": 0.42}},
    ]
    # The queue is empty after a drain.
    assert drain_events(inst)["events"] == []


# ── The clip-listener leak ─────────────────────────────────────────

def _set_slot_clip(song, track_index, slot_index, clip):
    slot = song.tracks[track_index].clip_slots[slot_index]
    slot.clip = clip
    slot.has_clip = clip is not None
    slot.fire("has_clip")


def test_slot_toggles_do_not_accumulate_clip_listeners():
    song, clip_a, _b = build_song()
    inst = make_instance(song)
    enable(inst)
    slot = song.tracks[0].clip_slots[0]
    assert clip_a.listener_count() == 3
    assert len(inst._passive_clip_bindings) == 2  # both occupied slots

    # Remove the clip, then add a different one, then swap directly -
    # the three paths that used to leak three listeners per toggle.
    _set_slot_clip(song, 0, 0, None)
    assert clip_a.listener_count() == 0
    assert (0, 0) not in inst._passive_clip_bindings

    clip_c = FakeClip("Clip C")
    _set_slot_clip(song, 0, 0, clip_c)
    assert clip_c.listener_count() == 3
    assert clip_a.listener_count() == 0

    clip_d = FakeClip("Clip D")
    _set_slot_clip(song, 0, 0, clip_d)
    assert clip_c.listener_count() == 0
    assert clip_d.listener_count() == 3

    # Repeated empty/fill cycles: counts stay flat, registry stays 1:1
    # with occupied slots, total census is invariant.
    for _ in range(5):
        _set_slot_clip(song, 0, 0, None)
        _set_slot_clip(song, 0, 0, clip_d)
    assert clip_d.listener_count() == 3
    assert len(inst._passive_clip_bindings) == 2
    assert total_listeners(song) == EXPECTED_CENSUS
    assert inst._passive_listener_count() == EXPECTED_CENSUS
    assert slot.listener_count() == 1


def test_track_removal_releases_its_clip_listeners():
    song, _a, clip_b = build_song()
    inst = make_instance(song)
    enable(inst)
    song.fire("tracks")  # prime the track count (first change has no previous)
    drain_events(inst)
    removed = song.tracks.pop(1)
    song.fire("tracks")  # on_tracks -> rebind
    assert removed.listener_count() == 0
    assert removed.mixer_device.volume.listener_count() == 0
    assert removed.clip_slots[0].listener_count() == 0
    assert clip_b.listener_count() == 0
    assert all(key[0] == 0 for key in inst._passive_clip_bindings)
    # The surviving track is untouched, and a tracks_changed event flowed.
    assert song.tracks[0].listener_count() == 5
    events = drain_events(inst)["events"]
    assert [e["type"] for e in events] == ["tracks_changed"]
    assert events[0]["detail"]["track_count"] == 1
    assert events[0]["detail"]["removed"] is True


# ── Coalescing ─────────────────────────────────────────────────────

def test_value_events_coalesce_latest_wins():
    song, _a, _b = build_song()
    inst = make_instance(song)
    enable(inst)
    track_a = song.tracks[0]
    volume = track_a.mixer_device.volume
    for i in range(50):
        volume.value = i / 100.0
        volume.fire("value")
    panning = track_a.mixer_device.panning
    for value in (-0.5, 0.0, 0.75):
        panning.value = value
        panning.fire("value")
    for tempo in (121.0, 122.0, 123.0):
        song.tempo = tempo
        song.fire("tempo")
    result = drain_events(inst)
    assert strip_ts(result["events"]) == [
        {"type": "volume_changed", "track_index": 0, "clip_index": None,
         "detail": {"volume": 0.49}},
        {"type": "panning_changed", "track_index": 0, "clip_index": None,
         "detail": {"panning": 0.75}},
        {"type": "tempo_changed", "track_index": None, "clip_index": None,
         "detail": {"tempo": 123.0}},
    ]


def test_discrete_events_never_coalesce():
    song, _a, _b = build_song()
    inst = make_instance(song)
    enable(inst)
    track_a = song.tracks[0]
    for name in ("One", "Two", "Three"):
        track_a.name = name
        track_a.fire("name")
    events = drain_events(inst)["events"]
    assert [e["detail"]["name"] for e in events] == ["One", "Two", "Three"]


def test_coalescing_is_per_source():
    song, _a, _b = build_song()
    inst = make_instance(song)
    enable(inst)
    for track in song.tracks:
        track.mixer_device.volume.value = 0.1
        track.mixer_device.volume.fire("value")
    events = drain_events(inst)["events"]
    assert [e["track_index"] for e in events] == [0, 1]


# ── Bounded queue: drop-oldest, counted ────────────────────────────

def test_queue_bound_drops_oldest_and_counts():
    song, _a, _b = build_song()
    inst = make_instance(song)
    assert isinstance(inst._passive_events, deque)
    inst._passive_max = 10
    for i in range(25):
        inst._enqueue_passive("name_changed", {"i": i}, track_index=0)
    assert len(inst._passive_events) == 10
    result = drain_events(inst)
    assert result["count"] == 10
    assert result["dropped"] == 15
    assert [e["detail"]["i"] for e in result["events"]] == \
        list(range(15, 25))
    # The counter resets on drain.
    assert drain_events(inst)["dropped"] == 0


def test_queue_at_cap_enqueue_stays_flat():
    """20k events at the cap: the deque never grows past it (the old
    list slice copied all 500 entries per event once full)."""
    song, _a, _b = build_song()
    inst = make_instance(song)
    for i in range(20000):
        inst._enqueue_passive("name_changed", {"i": i}, track_index=0)
    assert len(inst._passive_events) == script_module.PASSIVE_EVENT_QUEUE_MAX
    result = drain_events(inst)
    assert result["dropped"] == 20000 - script_module.PASSIVE_EVENT_QUEUE_MAX


# ── Server side: the poller negotiates; the tool surface ──────────

class _FakeConn(object):
    def __init__(self):
        self.calls = []

    def send_command(self, command_type, params=None):
        self.calls.append((command_type, params))
        if command_type == "drain_passive_events":
            return {"events": [], "count": 0, "dropped": 0}
        return {"enabled": True, "changed": True, "listener_count": 28}


class _FakeRecorder(object):
    def sweep_implicit_preferences(self):
        pass

    def record_passive_event(self, **kwargs):
        raise AssertionError("no events expected in these tests")


def _poller_with_caps(monkeypatch, caps):
    import MCP_Server.dataset.passive_poller as pp
    import MCP_Server.script_handshake as sh
    import MCP_Server.server as server

    conn = _FakeConn()
    monkeypatch.setattr(pp, "dataset_enabled", lambda: True)
    monkeypatch.setattr(pp, "get_recorder", lambda: _FakeRecorder())
    monkeypatch.setattr(sh, "script_has_capability",
                        lambda name: name in caps)
    monkeypatch.setattr(server, "get_ableton_connection", lambda: conn)
    return pp.PassiveEventPoller(), conn


def test_poller_enables_then_drains_and_releases_on_stop(monkeypatch):
    poller, conn = _poller_with_caps(
        monkeypatch, {"drain_passive_events", "set_passive_listeners"})
    poller._tick()
    assert conn.calls == [
        ("set_passive_listeners", {"enabled": True}),
        ("drain_passive_events", None),
    ]
    # Settled: later ticks only drain.
    poller._tick()
    assert conn.calls[-1] == ("drain_passive_events", None)
    assert len(conn.calls) == 3
    # Stop releases the registration it negotiated.
    poller.stop()
    assert conn.calls[-1] == ("set_passive_listeners", {"enabled": False})


def test_poller_leaves_legacy_script_alone(monkeypatch):
    poller, conn = _poller_with_caps(monkeypatch, {"drain_passive_events"})
    poller._tick()
    assert conn.calls == [("drain_passive_events", None)]
    poller.stop()
    assert conn.calls == [("drain_passive_events", None)]


_TOOL_SNIPPET = r"""
import asyncio, json, os, sys
sys.path.insert(0, os.environ["REPO_ROOT"])
import MCP_Server.server as server

calls = []
class FakeConn:
    def send_command(self, command_type, params=None):
        calls.append([command_type, params])
        return {"enabled": True, "changed": True, "listener_count": 28}
server.get_ableton_connection = lambda: FakeConn()

out = {}
async def main():
    tools = await server.mcp.list_tools()
    out["schemas"] = {t.name: t.inputSchema for t in tools}
asyncio.run(main())

server.set_passive_listeners(None)
out["default_call"] = calls[-1]
server.set_passive_listeners(None, enabled=False)
out["disable_call"] = calls[-1]

class OldScriptConn:
    def send_command(self, command_type, params=None):
        raise Exception("Unknown command: " + command_type)
server.get_ableton_connection = lambda: OldScriptConn()
out["old_script"] = server.set_passive_listeners(None)
print(json.dumps(out))
"""


def test_tool_surface_and_old_script_behaviour():
    env = dict(os.environ, REPO_ROOT=str(REPO_ROOT),
               ABLETON_MCP_LEAN_PROFILE="1")
    proc = subprocess.run(
        [sys.executable, "-c", _TOOL_SNIPPET], capture_output=True,
        text=True, env=env, cwd=str(REPO_ROOT))
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])

    assert out["default_call"] == [
        "set_passive_listeners", {"enabled": True}]
    assert out["disable_call"] == [
        "set_passive_listeners", {"enabled": False}]
    schema = out["schemas"]["set_passive_listeners"]
    assert schema["properties"]["enabled"]["default"] is True
    # A script without the command answers Unknown command, surfaced
    # as the standard Error string - no raise.
    assert out["old_script"] == (
        "Error setting passive listeners: "
        "Unknown command: set_passive_listeners")
