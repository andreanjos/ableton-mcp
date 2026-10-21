"""MCP-02 tests: the execute_batch wire command.

A batch sends an ordered list of commands in ONE socket round trip and
runs them inside ONE main-thread scheduling, returning the per-item
response envelopes a direct call would have produced, plus a count
summary. These tests pin:

* one schedule_message hop per batch, however many items it holds,
* per-item results in order, including mixed success/error,
* stop_on_error skip markers (never silent drops),
* nested execute_batch rejected as a per-item error,
* unknown command types as per-item errors, not batch crashes,
* timing opt-in composing with MCP-01 at both levels,
* AbletonConnection.send_batch: one send, batch result unwrapped,
  per-item failures do not raise,
* end to end over a real TCP socket: 42 sequential commands are 42
  requests; the same 42 as one batch are exactly 1 request.

The Remote Script half is loaded with _Framework stubbed, like
tests/test_mcp01_timing.py. No Ableton.
"""

import importlib.util
import json
import socket
import sys
import threading
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
        "ableton_mcp_remote_script_mcp02", BUNDLED_SCRIPT)
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
    # Count schedule_message calls; run tasks inline, as Live would on
    # its main thread.
    inst.schedule_calls = []

    def schedule(delay, fn):
        inst.schedule_calls.append(delay)
        fn()
    inst.schedule_message = schedule
    return inst


def stub_core_impls(inst):
    """Fast stand-ins for the handlers the batch tests exercise."""
    inst._get_session_info = lambda: {"tempo": 120.0}
    inst._set_tempo = lambda tempo: {"tempo": tempo}
    inst._create_midi_track = lambda index: {"track_index": index}
    inst._set_track_name = lambda ti, name: {"name": name}
    return inst


def batch_command(items, **extra):
    command = {"type": "execute_batch", "params": {"commands": items}}
    command.update(extra)
    return command


# ------------------------------------------------------- script side

def test_batch_runs_every_item_in_one_scheduling(script_module):
    inst = stub_core_impls(make_instance(script_module))
    items = [
        {"type": "get_session_info"},
        {"type": "set_tempo", "params": {"tempo": 121.0}},
        {"type": "create_midi_track", "params": {"index": -1}},
        {"type": "set_track_name", "params": {"track_index": 0, "name": "A"}},
        {"type": "set_tempo", "params": {"tempo": 122.0}},
        {"type": "get_session_info"},
    ]
    response = inst._process_command(batch_command(items))
    assert response["status"] == "success"
    result = response["result"]
    assert result["summary"] == {"total": 6, "succeeded": 6,
                                 "failed": 0, "skipped": 0}
    assert [r["status"] for r in result["results"]] == ["success"] * 6
    assert result["results"][1]["result"] == {"tempo": 121.0}
    assert result["results"][3]["result"] == {"name": "A"}
    # THE point of the feature: one main-thread scheduling for all six
    # commands - three of which are main-thread commands - not one each.
    assert len(inst.schedule_calls) == 1
    # Untimed batch: per-item envelopes carry no timing keys.
    assert all("agent0_timing" not in r for r in result["results"])


def test_batch_item_envelopes_match_direct_calls(script_module):
    inst = stub_core_impls(make_instance(script_module))
    direct = inst._process_command(
        {"type": "set_tempo", "params": {"tempo": 121.0}})
    batched = inst._process_command(batch_command(
        [{"type": "set_tempo", "params": {"tempo": 121.0}}]))
    assert batched["result"]["results"][0] == direct


def test_batch_mixed_success_and_error_keeps_order(script_module):
    inst = stub_core_impls(make_instance(script_module))

    def tempo_or_boom(tempo):
        if tempo == 999.0:
            raise RuntimeError("no such tempo")
        return {"tempo": tempo}
    inst._set_tempo = tempo_or_boom
    items = [
        {"type": "set_tempo", "params": {"tempo": 121.0}},
        {"type": "set_tempo", "params": {"tempo": 999.0}},
        {"type": "no_such_command"},
        {"type": "get_session_info"},
    ]
    response = inst._process_command(batch_command(items))
    assert response["status"] == "success"  # item failures are not batch failures
    results = response["result"]["results"]
    assert [r["status"] for r in results] == [
        "success", "error", "error", "success"]
    assert "no such tempo" in results[1]["message"]
    assert "Unknown command" in results[2]["message"]
    assert results[3]["result"] == {"tempo": 120.0}  # ran after the failures
    assert response["result"]["summary"] == {
        "total": 4, "succeeded": 2, "failed": 2, "skipped": 0}


def test_batch_stop_on_error_marks_the_rest_skipped(script_module):
    inst = stub_core_impls(make_instance(script_module))
    ran = []
    inst._set_track_name = lambda ti, name: ran.append(name) or {"name": name}

    def boom(tempo):
        raise RuntimeError("halt")
    inst._set_tempo = boom
    items = [
        {"type": "get_session_info"},
        {"type": "set_tempo", "params": {"tempo": 1.0}},
        {"type": "set_track_name", "params": {"track_index": 0, "name": "X"}},
        {"type": "set_track_name", "params": {"track_index": 1, "name": "Y"}},
    ]
    response = inst._process_command(
        {"type": "execute_batch",
         "params": {"commands": items, "stop_on_error": True}})
    results = response["result"]["results"]
    assert [r["status"] for r in results] == [
        "success", "error", "skipped", "skipped"]
    assert all("stop_on_error" in r["message"] for r in results[2:])
    assert ran == []  # the skipped items were NOT executed
    assert response["result"]["summary"] == {
        "total": 4, "succeeded": 1, "failed": 1, "skipped": 2}


def test_batch_nested_execute_batch_is_an_item_error(script_module):
    inst = stub_core_impls(make_instance(script_module))
    items = [
        {"type": "execute_batch",
         "params": {"commands": [{"type": "get_session_info"}]}},
        {"type": "get_session_info"},
    ]
    response = inst._process_command(batch_command(items))
    results = response["result"]["results"]
    assert results[0]["status"] == "error"
    assert "Nested execute_batch" in results[0]["message"]
    assert results[1]["status"] == "success"
    assert response["result"]["summary"]["failed"] == 1


def test_batch_requires_a_commands_list(script_module):
    inst = stub_core_impls(make_instance(script_module))
    for params in ({}, {"commands": "get_session_info"}, {"commands": 7}):
        response = inst._process_command(
            {"type": "execute_batch", "params": params})
        assert response["status"] == "error"
        assert "commands" in response["message"]


def test_batch_item_shape_errors_are_per_item(script_module):
    inst = stub_core_impls(make_instance(script_module))
    items = [
        42,
        {"type": "get_session_info", "params": ["not", "a", "dict"]},
        {"type": "get_session_info"},
    ]
    response = inst._process_command(batch_command(items))
    results = response["result"]["results"]
    assert [r["status"] for r in results] == ["error", "error", "success"]
    assert response["result"]["summary"] == {
        "total": 3, "succeeded": 1, "failed": 2, "skipped": 0}


def test_batch_empty_list_is_a_noop_success(script_module):
    inst = stub_core_impls(make_instance(script_module))
    response = inst._process_command(batch_command([]))
    assert response["status"] == "success"
    assert response["result"] == {
        "results": [],
        "summary": {"total": 0, "succeeded": 0, "failed": 0, "skipped": 0}}


def test_batch_timing_opt_in_covers_batch_and_items(script_module):
    inst = stub_core_impls(make_instance(script_module))
    items = [
        {"type": "get_session_info"},
        {"type": "set_tempo", "params": {"tempo": 121.0}},
    ]
    response = inst._process_command(batch_command(items, request_id=11))
    assert response["request_id"] == 11
    timing = response["agent0_timing"]
    assert timing["queue_ms"] is not None     # the one batch scheduling
    assert timing["execution_ms"] is not None
    for item_result in response["result"]["results"]:
        item_timing = item_result["agent0_timing"]
        assert item_timing["dispatch_ms"] >= 0
    # Direct-thread item: no queue of its own; scheduled item: timed run.
    assert response["result"]["results"][0]["agent0_timing"]["queue_ms"] is None
    assert response["result"]["results"][1]["agent0_timing"]["execution_ms"] is not None


def test_batch_advertised_in_handshake(script_module):
    inst = make_instance(script_module)
    assert "execute_batch" in script_module.SCRIPT_CAPABILITIES
    info = inst._get_script_info()
    assert "execute_batch" in info["capabilities"]
    assert info["script_version"] == "1.7.3"


# ------------------------------------------------------- server side

class FakeSock:
    """Canned-response socket, as in tests/test_mcp01_timing.py."""

    def __init__(self, response_obj):
        self._data = json.dumps(response_obj).encode("utf-8")
        self._i = 0
        self.sent = []
        self.timeouts = []

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, _size):
        if self._i >= len(self._data):
            return b""
        chunk = self._data[self._i:self._i + 5]
        self._i += len(chunk)
        return chunk


def _connection(response_obj):
    from MCP_Server.server import AbletonConnection
    conn = AbletonConnection("127.0.0.1", 9877)
    conn.sock = FakeSock(response_obj)
    return conn


BATCH_RESULT = {
    "results": [{"status": "success", "result": {"tempo": 121.0}},
                {"status": "error", "message": "Unknown command: nope"}],
    "summary": {"total": 2, "succeeded": 1, "failed": 1, "skipped": 0},
}
BATCH_ITEMS = [{"type": "set_tempo", "params": {"tempo": 121.0}},
               {"type": "nope"}]


def test_send_batch_is_one_send_and_unwraps_the_result():
    conn = _connection({"status": "success", "result": BATCH_RESULT})
    result = conn.send_batch(BATCH_ITEMS)
    # Per-item failure inside the batch does NOT raise.
    assert result == BATCH_RESULT
    assert len(conn.sock.sent) == 1
    sent = json.loads(conn.sock.sent[0].decode("utf-8"))
    assert sent == {"type": "execute_batch",
                    "params": {"commands": BATCH_ITEMS,
                               "stop_on_error": False}}


def test_send_batch_stop_on_error_and_timeout_scale_with_items():
    conn = _connection({"status": "success", "result": BATCH_RESULT})
    conn.send_batch(BATCH_ITEMS, stop_on_error=True)
    sent = json.loads(conn.sock.sent[0].decode("utf-8"))
    assert sent["params"]["stop_on_error"] is True
    # Two small items: more than a single modifying command's 15 s,
    # less than the sum with slack absurdity. timeouts[0] is the value
    # _send_command_locked selected; receive_full_response then applies
    # its own hard 15 s override (a pre-existing defect, MCP-09's scope,
    # which equally caps create_audio_clip's 65 s today).
    assert 15.0 < conn.sock.timeouts[0] <= 40.0


def test_send_batch_batch_level_error_raises():
    conn = _connection({"status": "error",
                        "message": "execute_batch requires a 'commands' list"})
    with pytest.raises(Exception) as excinfo:
        conn.send_batch(BATCH_ITEMS)
    assert "commands" in str(excinfo.value)


def test_send_batch_rejects_non_list_before_send():
    conn = _connection({"status": "success", "result": {}})
    with pytest.raises(ValueError):
        conn.send_batch("set_tempo")
    assert conn.sock.sent == []


# ------------------------------------------------------- end to end

EIGHT_TRACK_WORKFLOW = (
    ["get_session_snapshot"]
    + ["get_session_info", "create_midi_track", "set_track_name",
       "load_browser_item", "get_track_info"] * 8
    + ["get_session_snapshot"])


class ScriptPeer:
    """A real Remote Script instance behind a real TCP socket.

    Counts top-level requests: nested _process_command calls made by
    execute_batch items are the same method, so the counter tracks
    recursion depth and only counts calls that arrive off the wire.
    """

    def __init__(self, script_module):
        inst = make_instance(script_module)
        inst.running = True
        inst._get_session_info = lambda: {}
        inst._get_session_snapshot = lambda include_notes=True, include_params=True: {}
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


def test_batch_is_one_round_trip_regardless_of_item_count(script_module):
    from MCP_Server.server import AbletonConnection
    peer = ScriptPeer(script_module)
    try:
        conn = AbletonConnection("127.0.0.1", peer.port)
        for command_type in EIGHT_TRACK_WORKFLOW:
            conn.send_command(command_type, {})
        assert peer.requests == 42
        assert peer.connections == 1
        conn.disconnect()

        conn = AbletonConnection("127.0.0.1", peer.port)
        items = [{"type": t, "params": {}} for t in EIGHT_TRACK_WORKFLOW]
        result = conn.send_batch(items)
        conn.disconnect()
        assert peer.requests == 43          # exactly ONE more request
        assert peer.connections == 2
        assert result["summary"] == {"total": 42, "succeeded": 42,
                                     "failed": 0, "skipped": 0}
        assert len(result["results"]) == 42
    finally:
        peer.close()
