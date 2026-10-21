"""Headless protocol benchmark for the AbletonMCP fork (MCP-01).

Measures, without Ableton Live, the three protocol-level costs the lane
audit quantified ad hoc, so future performance PRs can paste a
before/after table produced by this script:

  A. Framing / repeated-parse cost. Under protocol v1 both fork
     endpoints receive by re-joining and re-parsing the whole
     accumulated buffer after every 8192-byte chunk. Section A runs
     the *exact* fork receive code (``AbletonConnection``'s v1
     ``receive_full_response`` and its MCP-03 ``_receive_framed_response``
     on the server side, ``AbletonMCP._handle_client`` speaking v1 and
     v2 on the Remote Script side) against fake sockets delivering
     fixed 8192-byte chunks, beside a one-shot ``json.loads`` of the
     same bytes. The v2 columns are the after picture for MCP-03: the
     same payload as one length-prefixed frame, parsed incrementally.

  B. Round trips per composite workflow. A fake TCP peer speaking
     protocol v1 counts requests and connections while a real
     ``AbletonConnection`` replays the command sequences the audit
     counted in section 4.4 of the lane audit. The point of the table is
     the counts (connections must stay 1), not the fake-peer latency.
     B2 replays the eight-track build against the *real* Remote Script
     dispatcher (stub handlers, real socket): once sequentially, once
     as a single execute_batch (MCP-02) - 42 round trips become 1.
     B3 replays the parameter tune (audit 4.4c) against the real
     dispatcher over a fake song: today's shape (one full-device read
     plus eight set_device_parameter calls - 9 wire requests) beside
     MCP-06's (one subset read plus one set_device_parameters call -
     2 wire requests), reporting wire requests and result bytes.

  C. Snapshot serialisation model. Builds the audit's synthetic
     field-for-field snapshot shape (per track: 2 devices x 20
     parameters, 16 session slots with one 64-note clip, one 64-note
     arrangement clip) and reports compact-encoded sizes and encode
     times for full (notes+params) vs lean snapshots. This is a model
     of the serializer's output shape, not a measured Live set.

  C2. Projected snapshot sizes. Runs the real Remote Script
     serializer against a fake Live object model at the section-C
     proportions and reports full vs MCP-05-projected snapshot sizes.

Run from the repo root:

    uv run python -m benchmarks.protocol_benchmark [--json]

Stdlib plus the fork's own modules; no Live, no network beyond loopback.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import socket
import statistics
import sys
import threading
import time
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

CHUNK = 8192
PAYLOAD_SIZES = [8192, 65536, 262144, 1048576, 2097152]


def _median_ms(fn, reps):
    samples = []
    for _ in range(reps):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000.0)
    return statistics.median(samples)


def _reps_for(size):
    if size >= 1048576:
        return 7
    if size >= 262144:
        return 15
    return 25


def _padded_json(size, key):
    """A single JSON object of exactly ``size`` bytes: {key: "xxx..."}. """
    base = json.dumps({key[0]: key[1], "pad": ""})
    pad_len = size - len(base.encode("utf-8"))
    assert pad_len >= 0
    return json.dumps({key[0]: key[1], "pad": "x" * pad_len}).encode("utf-8")


# --------------------------------------------------------------------------
# A1. Server receive path (exact AbletonConnection.receive_full_response)
# --------------------------------------------------------------------------

class _FakeSocket:
    """Hands out prepared chunks, then b"" (EOF)."""

    def __init__(self, payload, chunk=CHUNK):
        self._chunks = [payload[i:i + chunk]
                        for i in range(0, len(payload), chunk)]
        self._i = 0

    def settimeout(self, _timeout):
        pass

    def recv(self, _size):
        if self._i >= len(self._chunks):
            return b""
        chunk = self._chunks[self._i]
        self._i += 1
        return chunk


def bench_server_receive():
    from MCP_Server.framing import encode_frame
    from MCP_Server.server import AbletonConnection
    conn = AbletonConnection("127.0.0.1", 9)
    rows = []
    for size in PAYLOAD_SIZES:
        payload = _padded_json(size, ("status", "success"))
        assert len(payload) == size
        repeated = _median_ms(
            lambda: conn.receive_full_response(_FakeSocket(payload)),
            _reps_for(size))
        framed = encode_frame(payload)

        def receive_v2():
            data = conn._receive_framed_response(_FakeSocket(framed))
            return json.loads(data.decode("utf-8"))
        v2 = _median_ms(receive_v2, _reps_for(size))
        one_shot = _median_ms(lambda: json.loads(payload.decode("utf-8")),
                              _reps_for(size))
        rows.append({"bytes": size, "chunks": -(-size // CHUNK),
                     "repeated_parse_ms": repeated,
                     "one_shot_ms": one_shot,
                     "ratio": repeated / one_shot if one_shot else float("nan"),
                     "v2_ms": v2,
                     "v2_ratio": v2 / one_shot if one_shot else float("nan")})
    return rows


# --------------------------------------------------------------------------
# A2. Remote Script request path (exact AbletonMCP._handle_client)
# --------------------------------------------------------------------------

def _load_remote_script_module():
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
        "ableton_mcp_remote_script_benchmark",
        REPO_ROOT / "AbletonMCP_Remote_Script" / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeClient:
    def __init__(self, payload, chunk=CHUNK):
        self._chunks = [payload[i:i + chunk]
                        for i in range(0, len(payload), chunk)]
        self.sent = []

    def settimeout(self, _timeout):
        pass

    def recv(self, _size):
        return self._chunks.pop(0) if self._chunks else b""

    def sendall(self, data):
        self.sent.append(data)

    def close(self):
        pass


def bench_remote_receive():
    module = _load_remote_script_module()
    rows = []
    for size in PAYLOAD_SIZES:
        payload = _padded_json(size, ("type", "add_notes_to_clip"))
        assert len(payload) == size

        def run_once():
            inst = module.AbletonMCP.__new__(module.AbletonMCP)
            inst.log_message = lambda *a, **k: None
            inst.show_message = lambda *a, **k: None
            inst.running = True
            inst._process_command = lambda command: {
                "status": "success", "result": {}}
            inst._handle_client(_FakeClient(payload))

        framed = module._encode_frame(payload)

        def run_once_v2():
            inst = module.AbletonMCP.__new__(module.AbletonMCP)
            inst.log_message = lambda *a, **k: None
            inst.show_message = lambda *a, **k: None
            inst.running = True
            inst._process_command = lambda command: {
                "status": "success", "result": {}}
            inst._handle_client(_FakeClient(framed))

        repeated = _median_ms(run_once, _reps_for(size))
        v2 = _median_ms(run_once_v2, _reps_for(size))
        one_shot = _median_ms(lambda: json.loads(payload.decode("utf-8")),
                              _reps_for(size))
        rows.append({"bytes": size, "chunks": -(-size // CHUNK),
                     "repeated_parse_ms": repeated,
                     "one_shot_ms": one_shot,
                     "ratio": repeated / one_shot if one_shot else float("nan"),
                     "v2_ms": v2,
                     "v2_ratio": v2 / one_shot if one_shot else float("nan")})
    return rows


# --------------------------------------------------------------------------
# B. Round trips per composite workflow (fake TCP peer, real connection)
# --------------------------------------------------------------------------

class FakePeer:
    """Protocol-v1 peer: counts requests/connections, replies success.

    Echoes request_id and fabricates an agent0_timing block when a
    request carries one, matching the fork Remote Script's MCP-01
    behaviour, so the server-side timing capture is exercised too.
    """

    def __init__(self):
        self.requests = 0
        self.connections = 0
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(5)
        self.port = self._srv.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self):
        while True:
            try:
                client, _ = self._srv.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._serve, args=(client,),
                             daemon=True).start()

    def _serve(self, client):
        buffer = b""
        try:
            while True:
                data = client.recv(65536)
                if not data:
                    return
                buffer += data
                try:
                    command = json.loads(buffer.decode("utf-8"))
                except ValueError:
                    continue
                buffer = b""
                self.requests += 1
                response = {"status": "success", "result": {}}
                if "request_id" in command:
                    response["request_id"] = command["request_id"]
                    response["agent0_timing"] = {
                        "dispatch_ms": 0.0, "queue_ms": None,
                        "execution_ms": None}
                client.sendall(json.dumps(response).encode("utf-8"))
        except OSError:
            return
        finally:
            try:
                client.close()
            except OSError:
                pass

    def close(self):
        try:
            self._srv.close()
        except OSError:
            pass


WORKFLOWS = {
    "package one-track build (audit 4.4a)": (
        ["create_midi_track", "load_browser_item", "get_device_parameters"]
        + ["set_device_parameter"] * 8
        + ["create_clip", "add_notes_to_clip", "set_track_name",
           "set_clip_name"]),
    "direct eight-track build (audit 4.4b)": (
        ["get_session_snapshot"]
        + ["get_session_info", "create_midi_track", "set_track_name",
           "load_browser_item", "get_track_info"] * 8
        + ["get_session_snapshot"]),
    "parameter tune, 8 params (audit 4.4c)": (
        ["get_device_parameters"] + ["set_device_parameter"] * 8),
}


def bench_round_trips():
    from MCP_Server.server import AbletonConnection
    peer = FakePeer()
    rows = []
    try:
        for name, commands in WORKFLOWS.items():
            before_requests, before_conns = peer.requests, peer.connections
            conn = AbletonConnection("127.0.0.1", peer.port)
            start = time.perf_counter()
            for command_type in commands:
                conn.send_command(command_type, {})
            wall_ms = (time.perf_counter() - start) * 1000.0
            conn.disconnect()
            rows.append({
                "workflow": name,
                "commands": len(commands),
                "round_trips": peer.requests - before_requests,
                "connections": peer.connections - before_conns,
                "wall_ms": wall_ms,
            })
        # Timed exchange: request_id opt-in round-trips end to end.
        conn = AbletonConnection("127.0.0.1", peer.port)
        conn.send_command("get_session_info", {}, request_id=1)
        timing_captured = conn.last_timing is not None
        conn.disconnect()
    finally:
        peer.close()
    return rows, timing_captured


# --------------------------------------------------------------------------
# B2. The eight-track build as one execute_batch (MCP-02)
# --------------------------------------------------------------------------

class ScriptPeer:
    """A real Remote Script instance behind a real TCP socket.

    The batch scenario must execute in the fork's own dispatcher, not
    in a canned responder, so this peer serves the actual AbletonMCP
    class (loaded with _Framework stubbed). The handful of handlers
    the eight-track workflow touches are stubbed - their Live work is
    not the measurement; the request count is. Top-level requests are
    counted by recursion depth, because batch items call the same
    _process_command as wire commands do.
    """

    def __init__(self, module):
        inst = module.AbletonMCP.__new__(module.AbletonMCP)
        inst.log_message = lambda *a, **k: None
        inst.show_message = lambda *a, **k: None
        inst.schedule_message = lambda delay, fn: fn()
        inst.running = True
        inst._get_session_info = lambda: {}
        inst._get_session_snapshot = (
            lambda include_notes=True, include_params=True: {})
        inst._get_track_info = lambda track_index: {}
        inst._create_midi_track = lambda index: {}
        inst._set_track_name = lambda track_index, name: {}
        inst._load_browser_item = lambda track_index, item_uri: {}
        self.inst = inst
        self.requests = 0
        self.connections = 0
        self._depth = 0
        original = inst._process_command

        def counting(command):
            if self._depth == 0:
                self.requests += 1
            self._depth += 1
            try:
                return original(command)
            finally:
                self._depth -= 1
        inst._process_command = counting

        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(5)
        self.port = self._srv.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop,
                                        daemon=True)
        self._thread.start()

    def _accept_loop(self):
        while True:
            try:
                client, _ = self._srv.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self.inst._handle_client,
                             args=(client,), daemon=True).start()

    def close(self):
        try:
            self._srv.close()
        except OSError:
            pass


def bench_batch_round_trips():
    """The audit's 42-command eight-track build, sequential vs batched.

    Both runs replay the same command list through the real Remote
    Script dispatcher over a real AbletonConnection: first one
    send_command per command, then one send_batch for all of them.
    """
    from MCP_Server.server import AbletonConnection
    peer = ScriptPeer(_load_remote_script_module())
    rows = []
    try:
        commands = WORKFLOWS["direct eight-track build (audit 4.4b)"]
        before_requests, before_conns = peer.requests, peer.connections
        conn = AbletonConnection("127.0.0.1", peer.port)
        start = time.perf_counter()
        for command_type in commands:
            conn.send_command(command_type, {})
        wall_ms = (time.perf_counter() - start) * 1000.0
        conn.disconnect()
        rows.append({
            "workflow": "eight-track build, sequential (real script)",
            "commands": len(commands),
            "round_trips": peer.requests - before_requests,
            "connections": peer.connections - before_conns,
            "wall_ms": wall_ms,
        })
        before_requests, before_conns = peer.requests, peer.connections
        conn = AbletonConnection("127.0.0.1", peer.port)
        start = time.perf_counter()
        result = conn.send_batch(
            [{"type": t, "params": {}} for t in commands])
        wall_ms = (time.perf_counter() - start) * 1000.0
        conn.disconnect()
        rows.append({
            "workflow": "eight-track build, one execute_batch (MCP-02)",
            "commands": len(commands),
            "round_trips": peer.requests - before_requests,
            "connections": peer.connections - before_conns,
            "wall_ms": wall_ms,
            "items": result["summary"]["total"],
            "item_failures": result["summary"]["failed"],
        })
    finally:
        peer.close()
    return rows


# --------------------------------------------------------------------------
# B3. The parameter tune: sequential sets vs bulk set (MCP-06)
# --------------------------------------------------------------------------

class TunePeer:
    """A real Remote Script instance with a fake song, over a real socket.

    Unlike ScriptPeer (B2) nothing is stubbed: the device-parameter
    handlers run for real against the section-C2 fake object model, so
    the tune pays the real serialisation both ways. Top-level wire
    requests are counted by recursion depth, as in ScriptPeer.
    """

    def __init__(self, module):
        inst = module.AbletonMCP.__new__(module.AbletonMCP)
        inst.log_message = lambda *a, **k: None
        inst.show_message = lambda *a, **k: None
        inst.schedule_message = lambda delay, fn: fn()
        inst.running = True
        inst._song = _FakeSong(1)
        self.inst = inst
        self.requests = 0
        self.connections = 0
        self._depth = 0
        original = inst._process_command

        def counting(command):
            if self._depth == 0:
                self.requests += 1
            self._depth += 1
            try:
                return original(command)
            finally:
                self._depth -= 1
        inst._process_command = counting

        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(5)
        self.port = self._srv.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop,
                                        daemon=True)
        self._thread.start()

    def _accept_loop(self):
        while True:
            try:
                client, _ = self._srv.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self.inst._handle_client,
                             args=(client,), daemon=True).start()

    def close(self):
        try:
            self._srv.close()
        except OSError:
            pass


def bench_bulk_tune():
    """Read-then-tune 8 parameters on one device, both shapes.

    Today's shape replays audit 4.4c exactly: one full
    get_device_parameters read, then one scheduled set_device_parameter
    per parameter - 9 wire requests. The MCP-06 shape reads only the 8
    parameters it is about to tune (subset by name) and applies all 8
    writes as one set_device_parameters call - 2 wire requests. Result
    bytes are the JSON sizes of the unwrapped results the client
    receives, which is what the MCP layer then re-encodes.
    """
    from MCP_Server.server import AbletonConnection
    module = _load_remote_script_module()
    tune_indices = list(range(2, 10))
    tune_names = ["Param %d" % i for i in tune_indices]
    tune_values = [0.11 + 0.01 * i for i in range(8)]
    rows = []

    peer = TunePeer(module)
    try:
        conn = AbletonConnection("127.0.0.1", peer.port)
        start = time.perf_counter()
        result_bytes = 0
        read = conn.send_command(
            "get_device_parameters", {"track_index": 0, "device_index": 0})
        result_bytes += len(json.dumps(read).encode("utf-8"))
        applied = 0
        for index, value in zip(tune_indices, tune_values):
            result = conn.send_command(
                "set_device_parameter",
                {"track_index": 0, "device_index": 0,
                 "parameter_index": index, "value": value})
            result_bytes += len(json.dumps(result).encode("utf-8"))
            applied += 1
        wall_ms = (time.perf_counter() - start) * 1000.0
        conn.disconnect()
        rows.append({
            "workflow": "parameter tune, full read + 8 single sets",
            "wire_requests": peer.requests,
            "result_bytes": result_bytes,
            "connections": peer.connections,
            "sets_applied": applied,
            "wall_ms": wall_ms,
        })
    finally:
        peer.close()

    peer = TunePeer(module)
    try:
        conn = AbletonConnection("127.0.0.1", peer.port)
        start = time.perf_counter()
        result_bytes = 0
        read = conn.send_command(
            "get_device_parameters",
            {"track_index": 0, "device_index": 0,
             "parameter_names": tune_names})
        result_bytes += len(json.dumps(read).encode("utf-8"))
        result = conn.send_command(
            "set_device_parameters",
            {"track_index": 0, "device_index": 0,
             "parameters": [{"parameter_index": index, "value": value}
                            for index, value in
                            zip(tune_indices, tune_values)]})
        result_bytes += len(json.dumps(result).encode("utf-8"))
        wall_ms = (time.perf_counter() - start) * 1000.0
        conn.disconnect()
        rows.append({
            "workflow": "parameter tune, subset read + 1 bulk set (MCP-06)",
            "wire_requests": peer.requests,
            "result_bytes": result_bytes,
            "connections": peer.connections,
            "sets_applied": result["summary"]["succeeded"],
            "wall_ms": wall_ms,
        })
    finally:
        peer.close()
    return rows


# --------------------------------------------------------------------------
# C. Snapshot serialisation model (synthetic; audit 4.5 shape)
# --------------------------------------------------------------------------

def _notes(count):
    return [{"pitch": 60 + (i % 12), "start_time": i * 0.25,
             "duration": 0.25, "velocity": 100, "mute": False}
            for i in range(count)]


def _device(include_params):
    return {
        "name": "Device", "class_name": "PluginDevice", "type": "audio_effect",
        "parameters": (
            [{"name": "Param %d" % i, "value": 0.5, "min": 0.0, "max": 1.0,
              "is_enabled": True, "is_quantized": False,
              "value_string": "0.50", "automation_state": "none"}
             for i in range(20)] if include_params else []),
        "chains": [],
    }


def _clip(include_notes):
    clip = {"name": "Clip", "is_midi_clip": True, "length": 16.0}
    if include_notes:
        clip["notes"] = _notes(64)
    return clip


def synthetic_snapshot(track_count, include_notes, include_params):
    tracks = []
    for t in range(track_count):
        slots = [{"has_clip": False, "clip": None} for _ in range(16)]
        slots[0] = {"has_clip": True, "clip": _clip(include_notes)}
        tracks.append({
            "index": t, "name": "Track %d" % t, "is_foldable": False,
            "mute": False, "solo": False, "arm": False,
            "mixer_device": {"volume": 0.85, "panning": 0.0,
                             "sends": [0.0, 0.0]},
            "clip_slots": slots,
            "devices": [_device(include_params) for _ in range(2)],
            "arrangement_clips": [_clip(include_notes)],
        })
    return {"session": {"tempo": 120.0, "track_count": track_count},
            "tracks": tracks, "return_tracks": [], "master_track": {},
            "scenes": [], "cue_points": []}


def bench_snapshot_model():
    rows = []
    for track_count in (8, 32, 128):
        full = json.dumps(
            synthetic_snapshot(track_count, True, True)).encode("utf-8")
        lean = json.dumps(
            synthetic_snapshot(track_count, False, False)).encode("utf-8")
        full_ms = _median_ms(
            lambda: json.dumps(synthetic_snapshot(track_count, True, True)), 9)
        lean_ms = _median_ms(
            lambda: json.dumps(synthetic_snapshot(track_count, False, False)), 9)
        rows.append({"tracks": track_count, "full_bytes": len(full),
                     "lean_bytes": len(lean), "full_encode_ms": full_ms,
                     "lean_encode_ms": lean_ms})
    return rows



# --------------------------------------------------------------------------
# C2. Projected snapshot sizes (real serializer, fake LOM song; MCP-05)
# --------------------------------------------------------------------------
#
# Section C models the snapshot shape with synthetic dicts. This section
# instead runs the Remote Script serializer itself against a fake Live
# object model built to the audit 4.5 proportions (per track: 2 devices
# x 20 parameters with one automated parameter, 16 session slots with
# one 64-note clip, one 64-note arrangement clip, 2 sends; plus 2 return
# tracks, an 8-scene list and 2 cue points), and reports the encoded
# size of the full snapshot beside each MCP-05 projection.

class _FakeParam(object):
    def __init__(self, name, value=0.5, automated=False):
        self.name = name
        self.value = value
        self.min = 0.0
        self.max = 1.0
        self.is_enabled = True
        self.is_quantized = False
        self.value_string = "%.2f" % value
        self.automation_state = 1 if automated else 0


class _FakeDevice(object):
    def __init__(self, name, param_count=20):
        self.name = name
        self.class_name = "PluginDevice"
        self.class_display_name = "PluginDevice"
        self.can_have_chains = False
        self.can_have_drum_pads = False
        self.parameters = [
            _FakeParam("Param %d" % i, automated=(i == 0))
            for i in range(param_count)
        ]


class _FakeNote(object):
    def __init__(self, pitch, start_time):
        self.pitch = pitch
        self.start_time = start_time
        self.duration = 0.25
        self.velocity = 100
        self.mute = False


class _FakeClip(object):
    def __init__(self, name, note_count=64):
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
        self._note_list = [
            _FakeNote(60 + (i % 12), i * 0.25) for i in range(note_count)]

    def get_notes_extended(self, _from_pitch, _to_pitch, _start, _duration):
        return list(self._note_list)


class _FakeClipSlot(object):
    def __init__(self, clip=None):
        self.has_clip = clip is not None
        self.clip = clip


class _FakeMixer(object):
    def __init__(self, send_count=2):
        self.volume = _FakeParam("Volume", 0.85)
        self.panning = _FakeParam("Pan", 0.0)
        self.sends = [_FakeParam("Send %d" % i, 0.0)
                      for i in range(send_count)]


class _FakeTrack(object):
    def __init__(self, label, device_count=2, slot_count=16):
        self.name = label
        self.has_audio_input = False
        self.has_midi_input = True
        self.mute = False
        self.solo = False
        self.can_be_armed = True
        self.arm = False
        self.mixer_device = _FakeMixer()
        slots = [_FakeClipSlot() for _ in range(slot_count)]
        if slot_count:
            slots[0] = _FakeClipSlot(_FakeClip("Clip " + label))
        self.clip_slots = slots
        self.devices = [_FakeDevice("Device %d" % d)
                        for d in range(device_count)]
        self.arrangement_clips = [_FakeClip("Arr " + label)]


class _FakeScene(object):
    def __init__(self, index):
        self.name = "Scene %d" % index
        self.tempo = 120.0
        self.is_triggered = False


class _FakeCuePoint(object):
    def __init__(self, name, time):
        self.name = name
        self.time = time


class _FakeSong(object):
    def __init__(self, track_count):
        self.tempo = 120.0
        self.signature_numerator = 4
        self.signature_denominator = 4
        self.tracks = [_FakeTrack("Track %d" % i)
                       for i in range(track_count)]
        self.return_tracks = [
            _FakeTrack("Return %d" % i, device_count=1, slot_count=0)
            for i in range(2)]
        self.master_track = _FakeTrack("Master", device_count=1,
                                       slot_count=0)
        self.scenes = [_FakeScene(i) for i in range(8)]
        self.cue_points = [_FakeCuePoint("Intro", 0.0),
                           _FakeCuePoint("Drop", 32.0)]
        self.is_playing = False
        self.current_song_time = 0.0
        self.song_length = 64.0
        self.loop = False
        self.loop_start = 0.0
        self.loop_length = 0.0


_PROJECTION_VARIANTS = [
    ("full", {}),
    ("tracks_only", {"include_scenes": False,
                     "include_return_tracks": False,
                     "include_master_track": False,
                     "include_cue_points": False}),
    ("no_notes", {"include_notes": False}),
    ("no_params", {"include_params": False}),
    ("single_track", {"track_indices": [0]}),
]


def bench_projected_snapshots():
    module = _load_remote_script_module()
    rows = []
    for track_count in (24, 128):
        inst = module.AbletonMCP.__new__(module.AbletonMCP)
        inst._song = _FakeSong(track_count)
        inst.log_message = lambda *a, **k: None
        inst.show_message = lambda *a, **k: None
        for label, kwargs in _PROJECTION_VARIANTS:
            result = inst._get_session_snapshot(**kwargs)
            payload = json.dumps(result).encode("utf-8")
            rows.append({"tracks": track_count, "variant": label,
                         "bytes": len(payload)})
    return rows

# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def _print_parse_table(title, rows):
    print(title)
    print("  %10s %7s %14s %13s %12s %8s %8s" % (
        "bytes", "chunks", "v1 reparse ms", "v2 framed ms", "one-shot ms",
        "v1 ratio", "v2 ratio"))
    for r in rows:
        print("  %10d %7d %14.3f %13.3f %12.3f %7.1fx %7.2fx" % (
            r["bytes"], r["chunks"], r["repeated_parse_ms"], r["v2_ms"],
            r["one_shot_ms"], r["ratio"], r["v2_ratio"]))
    print()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true",
                        help="emit the results as JSON instead of tables")
    args = parser.parse_args(argv)

    logging.getLogger().setLevel(logging.CRITICAL)
    # MCP_Server.server calls logging.basicConfig(INFO) at import time;
    # silence its chatty per-command logger so the tables stay readable.
    logging.getLogger("AbletonMCPServer").setLevel(logging.CRITICAL)

    results = {
        "server_receive": bench_server_receive(),
        "remote_receive": bench_remote_receive(),
    }
    rows, timing_captured = bench_round_trips()
    results["round_trips"] = rows
    results["batch_round_trips"] = bench_batch_round_trips()
    results["bulk_tune"] = bench_bulk_tune()
    results["timing_captured"] = timing_captured
    results["snapshot_model"] = bench_snapshot_model()
    results["projected_snapshots"] = bench_projected_snapshots()

    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    print("AbletonMCP headless protocol benchmark (no Live; fake peers)\n")
    _print_parse_table(
        "A1. Server receive path - v1 receive_full_response vs "
        "MCP-03 _receive_framed_response:",
        results["server_receive"])
    _print_parse_table(
        "A2. Remote request path - AbletonMCP._handle_client "
        "(stub dispatcher):",
        results["remote_receive"])

    print("B. Round trips per composite workflow (fake TCP peer):")
    print("  %-42s %9s %12s %12s %10s" % ("workflow", "commands",
                                          "round trips", "connections",
                                          "wall ms"))
    for r in results["round_trips"]:
        print("  %-42s %9d %12d %12d %10.1f" % (
            r["workflow"], r["commands"], r["round_trips"],
            r["connections"], r["wall_ms"]))
    print("  request_id opt-in: agent0_timing captured by the server: %s"
          % ("yes" if timing_captured else "NO"))
    print()

    print("B2. Eight-track build against the real Remote Script "
          "dispatcher:")
    print("  %-42s %9s %12s %12s %10s" % ("workflow", "commands",
                                          "round trips", "connections",
                                          "wall ms"))
    for r in results["batch_round_trips"]:
        print("  %-42s %9d %12d %12d %10.1f" % (
            r["workflow"], r["commands"], r["round_trips"],
            r["connections"], r["wall_ms"]))
    batch_row = results["batch_round_trips"][-1]
    print("  batch items executed: %d, item failures: %d"
          % (batch_row["items"], batch_row["item_failures"]))
    print()

    print("B3. Parameter tune against the real Remote Script dispatcher:")
    print("  %-42s %13s %13s %12s %10s" % ("workflow", "wire requests",
                                          "result bytes", "connections",
                                          "wall ms"))
    for r in results["bulk_tune"]:
        print("  %-42s %13d %13d %12d %10.1f" % (
            r["workflow"], r["wire_requests"], r["result_bytes"],
            r["connections"], r["wall_ms"]))
    print("  sets applied per row: %s" % ", ".join(
        str(r["sets_applied"]) for r in results["bulk_tune"]))
    print()

    print("C. Snapshot serialisation model (synthetic, audit 4.5 shape):")
    print("  %7s %13s %13s %15s %15s" % ("tracks", "full bytes", "lean bytes",
                                        "full encode ms", "lean encode ms"))
    for r in results["snapshot_model"]:
        print("  %7d %13d %13d %15.3f %15.3f" % (
            r["tracks"], r["full_bytes"], r["lean_bytes"],
            r["full_encode_ms"], r["lean_encode_ms"]))
    print()

    print("C2. Projected snapshot sizes (real serializer, fake LOM):")
    print("  %7s %13s %13s %13s %13s %13s" % (
        "tracks", "full bytes", "tracks-only", "no-notes", "no-params",
        "single-track"))
    by_tracks = {}
    for r in results["projected_snapshots"]:
        by_tracks.setdefault(r["tracks"], {})[r["variant"]] = r["bytes"]
    for track_count in sorted(by_tracks):
        row = by_tracks[track_count]
        print("  %7d %13d %13d %13d %13d %13d" % (
            track_count, row["full"], row["tracks_only"], row["no_notes"],
            row["no_params"], row["single_track"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
