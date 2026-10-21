"""MCP-01 tests: native per-command timing, request IDs, derived
capabilities - and the benchmark harness smoke tests.

The timing feature absorbs Omnicorp's host-patch design (agent0_timing
{dispatch_ms, queue_ms, execution_ms}, optional echoed request_id,
complete dispatcher-derived capabilities) as a native fork feature.
Everything is opt-in per request: a command that carries no request_id
(and no "timing": true) must produce exactly the historical response
envelope - no extra keys, no behaviour change.

The Remote Script half is loaded with _Framework stubbed, like
tests/test_remote_script_bridge.py. No Ableton, no network (the fake
peer in the benchmark binds loopback on an ephemeral port).
"""

import ast
import importlib.util
import json
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
        "ableton_mcp_remote_script_mcp01", BUNDLED_SCRIPT)
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
    # Run scheduled main-thread tasks inline, as Live would on its thread.
    inst.schedule_message = lambda delay, fn: fn()
    return inst


# ------------------------------------------------------- capabilities

def _dispatcher_commands():
    """Derive the dispatcher's command set from source, as host_patch.py
    does: every string compared against command_type in _process_command."""
    tree = ast.parse(BUNDLED_SCRIPT.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "AbletonMCP")
    method = next(n for n in cls.body
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "_process_command")
    caps = set()
    for node in ast.walk(method):
        if (isinstance(node, ast.Compare)
                and isinstance(node.left, ast.Name)
                and node.left.id == "command_type"):
            for other in node.comparators:
                values = (other.elts if isinstance(other, (ast.List, ast.Tuple))
                          else [other])
                caps.update(v.value for v in values
                            if isinstance(v, ast.Constant)
                            and isinstance(v.value, str))
    return caps


def test_capabilities_are_the_complete_dispatcher_surface(script_module):
    derived = _dispatcher_commands()
    assert len(derived) == 42  # 37 base + execute_batch + set_device_parameters + search/refresh + set_passive_listeners
    assert set(script_module.SCRIPT_CAPABILITIES) == derived
    assert script_module.SCRIPT_CAPABILITIES == sorted(derived)


def test_handshake_advertises_complete_capabilities_and_markers(script_module):
    info = make_instance(script_module)._get_script_info()
    assert info["protocol_version"] == 1
    assert info["script_version"] == script_module.SCRIPT_VERSION == "1.7.8"
    assert info["capabilities"] == script_module.SCRIPT_CAPABILITIES
    assert len(info["capabilities"]) == 42
    assert info["agent0_request_ids"] is True
    assert info["agent0_capabilities_complete"] is True
    assert info["agent0_host_revision"] == 1


# ------------------------------------------------------- default path

def test_default_direct_command_envelope_is_unchanged(script_module):
    inst = make_instance(script_module)
    inst._get_session_info = lambda: {"tempo": 120.0}
    response = inst._process_command({"type": "get_session_info", "params": {}})
    assert response == {"status": "success", "result": {"tempo": 120.0}}


def test_default_scheduled_command_envelope_is_unchanged(script_module):
    inst = make_instance(script_module)
    inst._set_tempo = lambda tempo: {"tempo": tempo}
    response = inst._process_command(
        {"type": "set_tempo", "params": {"tempo": 121.0}})
    assert response == {"status": "success", "result": {"tempo": 121.0}}


# ------------------------------------------------------- opt-in timing

def test_request_id_on_direct_command_echoes_and_times(script_module):
    inst = make_instance(script_module)
    inst._get_session_info = lambda: {"tempo": 120.0}
    response = inst._process_command(
        {"type": "get_session_info", "params": {}, "request_id": 42})
    assert response["status"] == "success"
    assert response["request_id"] == 42
    timing = response["agent0_timing"]
    assert timing["dispatch_ms"] >= 0
    assert timing["queue_ms"] is None       # direct: no main-thread queue
    assert timing["execution_ms"] is None


def test_timing_flag_without_request_id(script_module):
    inst = make_instance(script_module)
    inst._get_session_info = lambda: {"tempo": 120.0}
    response = inst._process_command(
        {"type": "get_session_info", "params": {}, "timing": True})
    assert "agent0_timing" in response
    assert "request_id" not in response


def test_request_id_on_scheduled_command_splits_queue_and_execution(
        script_module):
    inst = make_instance(script_module)
    inst._set_tempo = lambda tempo: {"tempo": tempo}
    response = inst._process_command(
        {"type": "set_tempo", "params": {"tempo": 121.0}, "request_id": 7})
    assert response["request_id"] == 7
    assert response["result"] == {"tempo": 121.0}
    timing = response["agent0_timing"]
    assert timing["queue_ms"] is not None and timing["queue_ms"] >= 0
    assert timing["execution_ms"] is not None and timing["execution_ms"] >= 0
    assert timing["dispatch_ms"] >= timing["queue_ms"]


def test_request_id_on_set_device_parameter_path(script_module):
    """set_device_parameter has its own scheduling site; it must time too."""
    inst = make_instance(script_module)
    inst._set_device_parameter = lambda ti, di, pi, v: {"value": v}
    response = inst._process_command({
        "type": "set_device_parameter",
        "params": {"track_index": 0, "device_index": 0,
                   "parameter_index": 0, "value": 0.5},
        "request_id": 9})
    assert response["request_id"] == 9
    assert response["agent0_timing"]["queue_ms"] is not None
    assert response["agent0_timing"]["execution_ms"] is not None


def test_timing_attached_to_scheduled_error_responses(script_module):
    inst = make_instance(script_module)

    def boom(tempo):
        raise RuntimeError("no song")
    inst._set_tempo = boom
    response = inst._process_command(
        {"type": "set_tempo", "params": {"tempo": 1.0}, "request_id": 3})
    assert response["status"] == "error"
    assert response["request_id"] == 3
    assert response["agent0_timing"]["execution_ms"] is not None


@pytest.mark.parametrize("bad", [-1, 2 ** 53, "7", 1.5, True])
def test_invalid_request_id_rejected_before_dispatch(script_module, bad):
    inst = make_instance(script_module)
    called = []
    inst._get_session_info = lambda: called.append(True) or {}
    response = inst._process_command(
        {"type": "get_session_info", "params": {}, "request_id": bad})
    assert response == {"status": "error", "message": "Invalid request ID"}
    assert called == []


def test_boundary_request_ids_accepted(script_module):
    inst = make_instance(script_module)
    inst._get_session_info = lambda: {}
    for good in (0, 9007199254740991):
        response = inst._process_command(
            {"type": "get_session_info", "params": {}, "request_id": good})
        assert response["request_id"] == good


# --------------------------------------- installer (host_patch) anchors

def test_host_patch_anchor_strings_preserved():
    """Omnicorp's extension installer rewrites _process_command by exact
    string replacement (host_patch.py). Those anchors must survive the
    native implementation so the installer keeps applying to this script."""
    lines = BUNDLED_SCRIPT.read_text(encoding="utf-8").splitlines(keepends=True)
    tree = ast.parse("".join(lines))
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "AbletonMCP")
    method = next(n for n in cls.body
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "_process_command")
    seg = "".join(lines[method.lineno - 1:method.end_lineno])
    assert seg.count('        command_type = command.get("type", "")') == 1
    assert seg.count('response["result"] = self._get_script_info()') == 1
    assert seg.count("                def main_thread_task():\n") == 3  # + set_passive_listeners (MCP-08)
    assert seg.count("                        response_queue.put(") == 6  # 3 main-thread tasks x success/error
    assert seg.count("        return response\n") == 1


# ------------------------------------------------------- server side

class FakeSock:
    """Canned-response socket: records sends, serves the response in
    small slices so the real receive loop runs."""

    def __init__(self, response_obj):
        self._data = json.dumps(response_obj).encode("utf-8")
        self._i = 0
        self.sent = []

    def settimeout(self, _timeout):
        pass

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


def test_server_default_send_command_wire_shape_unchanged():
    conn = _connection({"status": "success", "result": {"ok": True}})
    result = conn.send_command("get_session_info")
    assert result == {"ok": True}
    sent = json.loads(conn.sock.sent[0].decode("utf-8"))
    assert sent == {"type": "get_session_info", "params": {}}
    assert conn.last_timing is None
    assert conn.last_request_id is None


def test_server_request_id_round_trip_captures_timing():
    timing = {"dispatch_ms": 1.0, "queue_ms": 0.5, "execution_ms": 0.25}
    conn = _connection({"status": "success", "result": {"ok": True},
                        "request_id": 5, "agent0_timing": timing})
    result = conn.send_command("get_session_info", {}, request_id=5)
    assert result == {"ok": True}
    sent = json.loads(conn.sock.sent[0].decode("utf-8"))
    assert sent["request_id"] == 5
    assert conn.last_timing == timing
    assert conn.last_request_id == 5


def test_server_request_id_mismatch_raises():
    conn = _connection({"status": "success", "result": {},
                        "request_id": 6, "agent0_timing": {}})
    with pytest.raises(Exception):
        conn.send_command("get_session_info", {}, request_id=5)


def test_server_invalid_request_id_rejected_before_send():
    conn = _connection({"status": "success", "result": {}})
    with pytest.raises(ValueError):
        conn.send_command("get_session_info", {}, request_id="5")
    assert conn.sock.sent == []


def test_server_error_envelope_still_raises_ableton_error():
    conn = _connection({"status": "error", "message": "boom",
                        "request_id": 5})
    with pytest.raises(Exception) as excinfo:
        conn.send_command("get_session_info", {}, request_id=5)
    assert "boom" in str(excinfo.value)


# ------------------------------------------------------- benchmark

def test_benchmark_round_trip_counts_match_the_audit():
    from benchmarks.protocol_benchmark import bench_round_trips
    rows, timing_captured = bench_round_trips()
    counts = {r["workflow"]: r for r in rows}
    assert counts["package one-track build (audit 4.4a)"]["round_trips"] == 15
    assert counts["direct eight-track build (audit 4.4b)"]["round_trips"] == 42
    assert counts["parameter tune, 8 params (audit 4.4c)"]["round_trips"] == 9
    assert all(r["connections"] == 1 for r in rows)
    assert timing_captured is True


def test_benchmark_snapshot_model_full_is_larger_than_lean():
    from benchmarks.protocol_benchmark import bench_snapshot_model
    rows = bench_snapshot_model()
    assert [r["tracks"] for r in rows] == [8, 32, 128]
    for r in rows:
        assert r["full_bytes"] > r["lean_bytes"] * 5


def test_benchmark_server_receive_runs(monkeypatch):
    import benchmarks.protocol_benchmark as pb
    monkeypatch.setattr(pb, "PAYLOAD_SIZES", [65536])
    rows = pb.bench_server_receive()
    assert rows[0]["bytes"] == 65536
    assert rows[0]["repeated_parse_ms"] > 0
    assert rows[0]["one_shot_ms"] > 0
