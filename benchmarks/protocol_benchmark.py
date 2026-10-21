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
     B6 replays simulated response gaps against the per-command
     timeout budgets (MCP-09): gaps beyond the old v1 receive path's
     hard 15 s override, inside the budgets the commands carry.
     B7 prices one main-thread scheduling on the script side
     (MCP-10): a handler-class command beside a main-class
     command through a fake scheduler, inline and with a
     modelled 1 ms servicing delay. Live's real servicing
     wait is not measurable headlessly (audit section 2.4);
     no row claims it.

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


# -- B4: browser browse-then-find (MCP-07) ---------------------------------

class _WalkCounter(object):
    """Counts hierarchy walks: every .children access on a fake
    browser item is one descent step a read pays inside Live."""

    def __init__(self):
        self.walks = 0


class _BenchBrowserItem(object):
    def __init__(self, counter, name, children=(), loadable=False,
                 device=False, uri=None):
        self._counter = counter
        self.name = name
        self._children = list(children)
        self.is_folder = bool(children)
        self.is_loadable = loadable
        self.is_device = device
        self.uri = uri

    @property
    def children(self):
        self._counter.walks += 1
        return self._children


class _BenchBrowser(object):
    """Plain-attribute browser: dir() sees only the five roots."""

    def __init__(self, instruments, sounds, drums, audio_effects,
                 midi_effects):
        self.instruments = instruments
        self.sounds = sounds
        self.drums = drums
        self.audio_effects = audio_effects
        self.midi_effects = midi_effects


def _bench_browser(counter):
    def item(name, **kw):
        return _BenchBrowserItem(counter, name, **kw)

    def folder(name, children):
        return _BenchBrowserItem(counter, name, children=children,
                                 uri="bench:" + name.lower())

    synths = folder("Synths", [
        item("Analog", loadable=True, device=True, uri="bench:analog"),
        item("Operator", loadable=True, device=True, uri="bench:operator"),
        item("Wavetable", loadable=True, device=True, uri="bench:wavetable"),
    ])
    keys = folder("Keys", [
        item("Grand Piano", loadable=True, uri="bench:grand-piano"),
        item("Electric Piano", loadable=True, uri="bench:electric-piano"),
    ])
    instruments = _BenchBrowserItem(
        counter, "Instruments",
        children=[synths, keys,
                  item("Simpler", loadable=True, device=True,
                       uri="bench:simpler")],
        uri="bench:instruments")
    kits = folder("Kits", [
        item("Kit %02d" % n, loadable=True, uri="bench:kit-%02d" % n)
        for n in range(1, 25)])
    drums = _BenchBrowserItem(counter, "Drums", children=[kits],
                              uri="bench:drums")
    sounds = _BenchBrowserItem(counter, "Sounds", children=[],
                               uri="bench:sounds")
    reverbs = folder("Reverbs", [
        item("Hall Reverb", loadable=True, device=True, uri="bench:hall"),
        item("Plate Reverb", loadable=True, device=True, uri="bench:plate"),
    ])
    audio_effects = _BenchBrowserItem(counter, "Audio Effects",
                                      children=[reverbs],
                                      uri="bench:audio-effects")
    midi_effects = _BenchBrowserItem(counter, "MIDI Effects", children=[],
                                     uri="bench:midi-effects")
    return _BenchBrowser(instruments, sounds, drums, audio_effects,
                         midi_effects)


class BrowserPeer(object):
    """The real Remote Script behind a real TCP socket, serving a
    counting fake browser as its application."""

    def __init__(self, script_module):
        self.module = script_module
        self.counter = _WalkCounter()
        app = types.SimpleNamespace(browser=_bench_browser(self.counter))
        inst = script_module.AbletonMCP.__new__(script_module.AbletonMCP)
        inst.log_message = lambda *args: None
        inst.running = True
        inst.application = lambda: app
        inst._clients_lock = threading.Lock()
        inst._clients = set()
        self.inst = inst
        self.wire_requests = 0
        process = inst._process_command
        depth = [0]

        def counting_process(request):
            if depth[0] == 0:
                self.wire_requests += 1
            depth[0] += 1
            try:
                return process(request)
            finally:
                depth[0] -= 1

        inst._process_command = counting_process
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        conn, _ = self._listener.accept()
        try:
            self.inst._handle_client(conn)
        finally:
            conn.close()

    def connect(self):
        from MCP_Server.server import AbletonConnection
        return AbletonConnection("127.0.0.1", self.port)

    def close(self):
        self._listener.close()


def bench_browser_browse():
    """B4 (MCP-07): a browse-then-find session against the real script.

    One peer runs a browse pass twice - cold caches, then warm, where
    every repeated read must be served without a single hierarchy
    walk. A second peer finds the items named "kit" the pre-MCP-07
    way (tree + per-folder item reads, filtered client-side) and then
    via search_browser. Hierarchy walks are .children accesses on the
    fake browser: the repeated work the caches and the search exist
    to remove.
    """
    mod = _load_remote_script_module()
    rows = []

    def browse_pass(conn):
        conn.send_command("get_browser_tree", {"category_type": "all"})
        for path in ("instruments", "instruments/Synths", "drums/Kits"):
            conn.send_command("get_browser_items_at_path", {"path": path})
        conn.send_command("get_browser_item", {"uri": "bench:kit-07"})

    peer = BrowserPeer(mod)
    try:
        conn = peer.connect()
        for label in ("browse pass, cold caches",
                      "browse pass, warm caches (MCP-07)"):
            reqs0, walks0 = peer.wire_requests, peer.counter.walks
            started = time.perf_counter()
            browse_pass(conn)
            rows.append({
                "workflow": label,
                "wire_requests": peer.wire_requests - reqs0,
                "hierarchy_walks": peer.counter.walks - walks0,
                "wall_ms": (time.perf_counter() - started) * 1000.0,
            })
        conn.disconnect()
    finally:
        peer.close()

    peer = BrowserPeer(mod)
    try:
        conn = peer.connect()
        reqs0, walks0 = peer.wire_requests, peer.counter.walks
        started = time.perf_counter()
        conn.send_command("get_browser_tree", {"category_type": "all"})
        found = 0
        for path in ("instruments", "instruments/Synths",
                     "instruments/Keys", "drums", "drums/Kits",
                     "audio_effects", "audio_effects/Reverbs"):
            result = conn.send_command("get_browser_items_at_path",
                                       {"path": path})
            found += sum(1 for it in result["items"]
                         if "kit" in it["name"].lower())
        rows.append({
            "workflow": "find 'kit' client-side (tree + folder reads)",
            "wire_requests": peer.wire_requests - reqs0,
            "hierarchy_walks": peer.counter.walks - walks0,
            "wall_ms": (time.perf_counter() - started) * 1000.0,
        })

        reqs0, walks0 = peer.wire_requests, peer.counter.walks
        started = time.perf_counter()
        offset = 0
        found = 0
        while offset is not None:
            page = conn.send_command(
                "search_browser",
                {"query": "kit", "limit": 10, "offset": offset})
            found += len(page["items"])
            offset = page["next_offset"]
        rows.append({
            "workflow": "find 'kit' via search_browser (3 pages)",
            "wire_requests": peer.wire_requests - reqs0,
            "hierarchy_walks": peer.counter.walks - walks0,
            "wall_ms": (time.perf_counter() - started) * 1000.0,
        })
        conn.disconnect()
    finally:
        peer.close()
    return rows


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


# -- B5: passive listeners + event queue (MCP-08) ---------------------------

class _BenchListenerHost(object):
    """Live-style counted listener registry (benchmark fakes)."""

    def __init__(self):
        self._listeners = {}

    def _add(self, kind, cb):
        self._listeners.setdefault(kind, []).append(cb)

    def _remove(self, kind, cb):
        lst = self._listeners.get(kind, [])
        if cb in lst:
            lst.remove(cb)

    def count(self):
        return sum(len(v) for v in self._listeners.values())

    def fire(self, kind):
        for cb in list(self._listeners.get(kind, [])):
            cb()


def _bench_listener_methods(kind):
    def add(self, cb):
        self._add(kind, cb)

    def remove(self, cb):
        self._remove(kind, cb)

    return add, remove


class _BenchParam(_BenchListenerHost):
    def __init__(self, value=0.5):
        super().__init__()
        self.value = value

    add_value_listener, remove_value_listener = \
        _bench_listener_methods("value")


class _BenchClip(_BenchListenerHost):
    def __init__(self, name):
        super().__init__()
        self.name = name
        self.is_playing = False

    add_name_listener, remove_name_listener = \
        _bench_listener_methods("name")
    add_notes_listener, remove_notes_listener = \
        _bench_listener_methods("notes")
    add_playing_status_listener, remove_playing_status_listener = \
        _bench_listener_methods("playing_status")


class _BenchSlot(_BenchListenerHost):
    def __init__(self, clip=None):
        super().__init__()
        self.has_clip = clip is not None
        self.clip = clip

    add_has_clip_listener, remove_has_clip_listener = \
        _bench_listener_methods("has_clip")


class _BenchMixer(object):
    def __init__(self):
        self.volume = _BenchParam(0.85)
        self.panning = _BenchParam(0.0)


class _BenchTrack(_BenchListenerHost):
    def __init__(self, name, slots):
        super().__init__()
        self.name = name
        self.mute = False
        self.solo = False
        self.arm = False
        self.can_be_armed = True
        self.devices = []
        self.clip_slots = list(slots)
        self.mixer_device = _BenchMixer()

    add_name_listener, remove_name_listener = \
        _bench_listener_methods("name")
    add_mute_listener, remove_mute_listener = \
        _bench_listener_methods("mute")
    add_solo_listener, remove_solo_listener = \
        _bench_listener_methods("solo")
    add_arm_listener, remove_arm_listener = _bench_listener_methods("arm")
    add_devices_listener, remove_devices_listener = \
        _bench_listener_methods("devices")


class _BenchSong(_BenchListenerHost):
    def __init__(self, tracks):
        super().__init__()
        self.tracks = list(tracks)
        self.tempo = 120.0
        self.signature_numerator = 4
        self.signature_denominator = 4
        self.is_playing = False

    add_tempo_listener, remove_tempo_listener = \
        _bench_listener_methods("tempo")
    add_signature_numerator_listener, \
        remove_signature_numerator_listener = \
        _bench_listener_methods("signature_numerator")
    add_signature_denominator_listener, \
        remove_signature_denominator_listener = \
        _bench_listener_methods("signature_denominator")
    add_is_playing_listener, remove_is_playing_listener = \
        _bench_listener_methods("is_playing")
    add_tracks_listener, remove_tracks_listener = \
        _bench_listener_methods("tracks")


def _bench_busy_song(track_count=8, slots_per_track=16):
    """A busy session: every other slot holds a clip."""
    tracks = []
    for t in range(track_count):
        slots = []
        for c in range(slots_per_track):
            clip = _BenchClip("Clip %d/%d" % (t, c)) if c % 2 == 0 else None
            slots.append(_BenchSlot(clip))
        tracks.append(_BenchTrack("Track %d" % t, slots))
    return _BenchSong(tracks)


def _bench_passive_instance(mod, song):
    from collections import deque
    inst = mod.AbletonMCP.__new__(mod.AbletonMCP)
    inst.log_message = lambda *a, **k: None
    inst.show_message = lambda *a, **k: None
    inst.schedule_message = lambda delay, fn: fn()
    inst._song = song
    inst._passive_events = deque()
    inst._passive_lock = threading.Lock()
    inst._passive_max = mod.PASSIVE_EVENT_QUEUE_MAX
    inst._passive_dropped = 0
    inst._passive_listeners_enabled = False
    inst._passive_track_count = None
    inst._passive_track_bindings = []
    inst._passive_clip_bindings = {}
    inst._song_passive_callbacks = []
    inst._dispatch_state = threading.local()
    return inst


def bench_passive_listeners():
    """B5 (MCP-08): listener census + event-queue behaviour.

    A busy session (8 tracks x 16 slots, half occupied) used to carry
    its full listener census from script startup, forever, whether or
    not any consumer ever drained an event. With negotiation the
    census exists only while a consumer has it enabled. The flood
    rows replay 5,000 fader moves on one track: pre-MCP-08 every move
    enqueued (the queue then sat at its 500 cap, re-slicing the whole
    list per event - the legacy enqueue below is those exact lines);
    MCP-08 coalesces the stream to the single latest value and the
    bounded deque appends in O(1).
    """
    mod = _load_remote_script_module()
    rows = []

    song = _bench_busy_song()
    inst = _bench_passive_instance(mod, song)
    rows.append({
        "scenario": "registered listeners, session never enables events",
        "listeners": inst._passive_listener_count(),
        "pending_events": 0,
        "enqueue_wall_ms": 0.0,
    })
    inst._set_passive_listeners(True)
    rows.append({
        "scenario": "registered listeners, enabled (= pre-MCP-08 always-on)",
        "listeners": inst._passive_listener_count(),
        "pending_events": 0,
        "enqueue_wall_ms": 0.0,
    })

    # Flood: 5,000 fader moves. Legacy pending count is definitional
    # (one append per move, capped at 500); MCP-08 is measured live.
    volume = song.tracks[0].mixer_device.volume
    started = time.perf_counter()
    for i in range(5000):
        volume.value = (i % 100) / 100.0
        volume.fire("value")
    flood_ms = (time.perf_counter() - started) * 1000.0
    rows.append({
        "scenario": "5,000 fader moves, MCP-08 coalesced queue",
        "listeners": inst._passive_listener_count(),
        "pending_events": len(inst._passive_events),
        "enqueue_wall_ms": flood_ms,
    })
    rows.append({
        "scenario": "5,000 fader moves, pre-MCP-08 queue (definitional)",
        "listeners": inst._passive_listener_count(),
        "pending_events": mod.PASSIVE_EVENT_QUEUE_MAX,
        "enqueue_wall_ms": 0.0,
    })

    # Enqueue cost at the cap: legacy list+slice vs the bounded deque,
    # 20,000 discrete events each.
    legacy_events = []
    started = time.perf_counter()
    for i in range(20000):
        legacy_events.append({"type": "name_changed", "i": i})
        if len(legacy_events) > mod.PASSIVE_EVENT_QUEUE_MAX:
            legacy_events = legacy_events[-mod.PASSIVE_EVENT_QUEUE_MAX:]
    legacy_ms = (time.perf_counter() - started) * 1000.0
    rows.append({
        "scenario": "20,000 events at cap, legacy list+slice enqueue",
        "listeners": 0,
        "pending_events": len(legacy_events),
        "enqueue_wall_ms": legacy_ms,
    })
    inst2 = _bench_passive_instance(mod, _bench_busy_song())
    started = time.perf_counter()
    for i in range(20000):
        inst2._enqueue_passive("name_changed", {"i": i}, track_index=0)
    deque_ms = (time.perf_counter() - started) * 1000.0
    rows.append({
        "scenario": "20,000 events at cap, MCP-08 deque enqueue",
        "listeners": 0,
        "pending_events": len(inst2._passive_events),
        "enqueue_wall_ms": deque_ms,
    })
    return rows


# --------------------------------------------------------------------------
# B6. Timeout budgets vs simulated response gaps (MCP-09)
# --------------------------------------------------------------------------

class _GapSocket:
    """Fake socket with a virtual response gap.

    recv() raises socket.timeout when the scripted gap exceeds the
    timeout in force - the contract a real socket offers - and serves
    the payload (in small slices) otherwise. Nothing really waits:
    the point is WHICH timeout value governs the exchange, not
    wall-clock latency.
    """

    def __init__(self, payload, gap):
        self._data = payload
        self._i = 0
        self._gap = gap
        self._timeout = None
        self._gap_done = False
        self.sent = []
        self.closed = False

    def settimeout(self, timeout):
        self._timeout = timeout

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, _size):
        if not self._gap_done:
            self._gap_done = True
            if self._timeout is not None and self._gap > self._timeout:
                raise socket.timeout("timed out (simulated gap)")
        if self._i >= len(self._data):
            return b""
        chunk = self._data[self._i:self._i + 5]
        self._i += len(chunk)
        return chunk

    def close(self):
        self.closed = True


def bench_timeout_budgets():
    """B6 (MCP-09): do per-command budgets survive the receive paths?

    Each live row runs a real AbletonConnection against a fake peer
    whose response gap is simulated at 20 s (see _GapSocket) - beyond
    the pre-MCP-09 v1 receive path's hard 15 s override, but within
    create_audio_clip's 65 s budget and a two-item batch's summed
    25 s. The legacy rows are definitional, like B5's: the old v1
    receive forced settimeout(15.0), so any gap over 15 s timed out
    whatever budget the command had selected. The read row pins the
    other direction: a gap beyond a command's own budget fails, now
    as OutcomeUnknownError instead of a plain timeout.
    """
    from MCP_Server.framing import encode_frame
    from MCP_Server.server import AbletonConnection, OutcomeUnknownError

    gap = 20.0
    rows = []

    def run(scenario, framing, budget, payload, framing_v2=False,
            command=None, batch=None):
        sock = _GapSocket(payload, gap)
        conn = AbletonConnection("127.0.0.1", 9)
        conn.sock = sock
        conn._framing_v2 = framing_v2
        try:
            if batch is not None:
                conn.send_batch(batch)
            else:
                conn.send_command(command)
            outcome = "completed"
        except OutcomeUnknownError:
            outcome = "OutcomeUnknownError"
        except Exception as e:  # pragma: no cover - defensive row fill
            outcome = type(e).__name__
        rows.append({"scenario": scenario, "framing": framing,
                     "budget_s": budget, "simulated_gap_s": gap,
                     "outcome": outcome})

    ok_v1 = json.dumps({"status": "success", "result": {}}).encode("utf-8")
    ok_v2 = encode_frame(json.dumps(
        {"status": "success", "result": {},
         "request_id": 1}).encode("utf-8"))
    batch_items = [{"type": "set_tempo", "params": {"tempo": 120.0}},
                   {"type": "get_session_info"}]
    batch_v1 = json.dumps({"status": "success", "result": {
        "results": [], "summary": {"total": 2, "succeeded": 2,
                                   "failed": 0,
                                   "skipped": 0}}}).encode("utf-8")

    run("create_audio_clip, 20 s gap (MCP-09)", "v1", 65.0, ok_v1,
        command="create_audio_clip")
    rows.append({"scenario": "create_audio_clip, legacy v1 receive "
                             "(definitional)",
                 "framing": "v1", "budget_s": 15.0, "simulated_gap_s": gap,
                 "outcome": "timed out at the 15 s cap"})
    run("create_audio_clip, 20 s gap (MCP-09)", "v2", 65.0, ok_v2,
        framing_v2=True, command="create_audio_clip")
    run("execute_batch x2, 20 s gap (MCP-09)", "v1", 25.0, batch_v1,
        batch=batch_items)
    rows.append({"scenario": "execute_batch x2, legacy v1 receive "
                             "(definitional)",
                 "framing": "v1", "budget_s": 15.0, "simulated_gap_s": gap,
                 "outcome": "timed out at the 15 s cap"})
    run("get_session_info, 20 s gap - beyond the read budget", "v1",
        10.0, ok_v1, command="get_session_info")
    return rows


# --------------------------------------------------------------------------
# B7. Dispatch scheduling cost (MCP-10)
# --------------------------------------------------------------------------

def bench_dispatch_scheduling():
    """B7 (MCP-10): the script-side cost of one main-thread scheduling.

    The MCP-10 audit classified all 42 dispatcher commands from
    their handlers: 27 "main" class (the handler touches the LOM, so
    the command pays one schedule_message hop) and 15 "handler" class
    (run on the calling thread). No scheduled command proved
    LOM-free, so none moved; what this section measures is what the
    hop itself costs on the script side - queue creation, the
    closure, the queue wake-up - under a FAKE scheduler that runs
    the task inline. The delayed row adds a modelled 1 ms servicing
    delay to show where Live's wait would enter the total; Live's
    real servicing wait depends on the main thread's load and is
    not measurable headlessly (audit section 2.4), and no row here
    claims it.
    """
    mod = _load_remote_script_module()
    rows = []

    def make_instance(schedule_message):
        inst = mod.AbletonMCP.__new__(mod.AbletonMCP)
        inst.log_message = lambda *a, **k: None
        inst.show_message = lambda *a, **k: None
        inst.schedule_message = schedule_message
        return inst

    reps = 200

    inst = make_instance(lambda delay, fn: fn())
    ms = _median_ms(
        lambda: inst._process_command(
            {"type": "get_script_info", "params": {}}), reps)
    rows.append({"scenario": "get_script_info dispatch (handler class)",
                 "thread_class": "handler", "schedule_hops": 0,
                 "median_wall_ms": ms})

    hops = {"n": 0}

    def inline_scheduler(delay, fn):
        hops["n"] += 1
        fn()

    inst = make_instance(inline_scheduler)
    inst._set_tempo = lambda tempo: {"tempo": tempo}
    ms = _median_ms(
        lambda: inst._process_command(
            {"type": "set_tempo", "params": {"tempo": 120.0}}), reps)
    rows.append({"scenario": "set_tempo dispatch, fake scheduler inline",
                 "thread_class": "main",
                 "schedule_hops": hops["n"] // reps,
                 "median_wall_ms": ms})

    def delayed_scheduler(delay, fn):
        time.sleep(0.001)  # modelled servicing delay - NOT a Live figure
        fn()

    inst = make_instance(delayed_scheduler)
    inst._set_tempo = lambda tempo: {"tempo": tempo}
    ms = _median_ms(
        lambda: inst._process_command(
            {"type": "set_tempo", "params": {"tempo": 120.0}}), 25)
    rows.append({"scenario": "set_tempo dispatch, +1 ms modelled delay",
                 "thread_class": "main", "schedule_hops": 1,
                 "median_wall_ms": ms})

    hops["n"] = 0
    inst = make_instance(inline_scheduler)
    batch = {"type": "execute_batch", "params": {"commands": [
        {"type": "get_script_info", "params": {}} for _ in range(8)]}}
    ms = _median_ms(lambda: inst._process_command(batch), reps)
    rows.append({"scenario": "execute_batch x8 handler items, inline",
                 "thread_class": "main",
                 "schedule_hops": hops["n"] // reps,
                 "median_wall_ms": ms})
    return rows


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
    results["browser_browse"] = bench_browser_browse()
    results["passive_listeners"] = bench_passive_listeners()
    results["timeout_budgets"] = bench_timeout_budgets()
    results["dispatch_scheduling"] = bench_dispatch_scheduling()
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

    print("B4. Browser browse-then-find against the real Remote "
          "Script dispatcher (MCP-07):")
    print("  %-42s %13s %16s %10s" % ("workflow", "wire requests",
                                     "hierarchy walks", "wall ms"))
    for r in results["browser_browse"]:
        print("  %-42s %13d %16d %10.1f" % (
            r["workflow"], r["wire_requests"], r["hierarchy_walks"],
            r["wall_ms"]))
    print()

    print("B5. Passive listeners + event queue (MCP-08):")
    print("  %-52s %10s %14s %13s" % ("scenario", "listeners",
                                     "pending events", "enqueue ms"))
    for r in results["passive_listeners"]:
        print("  %-52s %10d %14d %13.1f" % (
            r["scenario"], r["listeners"], r["pending_events"],
            r["enqueue_wall_ms"]))
    print()

    print("B6. Timeout budgets vs simulated response gaps (MCP-09;")
    print("    fake peer, gaps are virtual - no real waiting):")
    print("  %-52s %7s %9s %7s  %s" % ("scenario", "framing", "budget s",
                                     "gap s", "outcome"))
    for r in results["timeout_budgets"]:
        print("  %-52s %7s %9.0f %7.0f  %s" % (
            r["scenario"], r["framing"], r["budget_s"],
            r["simulated_gap_s"], r["outcome"]))
    print()

    print("B7. Dispatch scheduling (MCP-10; fake scheduler - Live's")
    print("    main-thread servicing wait is not measurable headlessly;")
    print("    the 1 ms row is a modelled delay, not a Live figure):")
    print("  %-52s %9s %6s %12s" % ("scenario", "class", "hops",
                                   "median ms"))
    for r in results["dispatch_scheduling"]:
        print("  %-52s %9s %6d %12.3f" % (
            r["scenario"], r["thread_class"], r["schedule_hops"],
            r["median_wall_ms"]))
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
