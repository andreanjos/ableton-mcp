"""MCP-03 tests: framing v2 - length-prefixed frames, native request
IDs, incremental parsers on both endpoints, negotiated over v1.

Wire format: 4-byte big-endian payload length + the UTF-8 JSON payload
(the same object v1 sends raw). Negotiation: the Remote Script
advertises agent0_framing: ["v1", "v2"] in get_script_info; the MCP
server uses v2 only on a connection whose handshake advertised it, and
the script sniffs each message's framing at its boundary and answers in
the framing the request used. The matrix pinned here:

* v2 server <-> v2 script: framed, native request ids, timing captured
* v2 server <-> v1 script: falls back to v1, byte-identical requests
* v1 client <-> v2 script: answered in v1, byte-identical responses
* execute_batch and the timing opt-in compose with v2 unchanged

Frame edge cases: split across tiny chunks, several frames coalesced in
one chunk, oversized length (framed error, no hang, no desync), invalid
JSON payload, non-object payload, garbage prefix (defined close).

The Remote Script half is loaded with _Framework stubbed, like
tests/test_mcp01_timing.py. No Ableton.
"""

import importlib.util
import json
import socket
import struct
import sys
import threading
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLED_SCRIPT = (REPO_ROOT / "MCP_Server" / "bundled_ableton_remote_script"
                  / "AbletonMCP_init.py")

from MCP_Server.framing import (MAX_FRAME_BYTES, FrameDecoder, FrameError,
                                encode_frame)


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
        "ableton_mcp_remote_script_mcp03", BUNDLED_SCRIPT)
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
    inst.schedule_calls = []

    def schedule(delay, fn):
        inst.schedule_calls.append(delay)
        fn()
    inst.schedule_message = schedule
    return inst


# ------------------------------------------------------- codecs

def test_shared_codec_round_trip_and_incremental_feed():
    payload = json.dumps({"type": "get_session_info", "params": {}}).encode()
    decoder = FrameDecoder()
    framed = encode_frame(payload)
    # Split inside the header, then inside the payload.
    decoder.feed(framed[:2])
    assert decoder.next_frame() is None
    decoder.feed(framed[2:5])
    assert decoder.next_frame() is None
    decoder.feed(framed[5:])
    assert decoder.next_frame() == payload
    assert decoder.pending_bytes == 0


def test_shared_codec_two_frames_in_one_feed():
    decoder = FrameDecoder()
    decoder.feed(encode_frame(b"{}") + encode_frame(b'{"a": 1}'))
    assert decoder.next_frame() == b"{}"
    assert decoder.next_frame() == b'{"a": 1}'
    assert decoder.next_frame() is None


def test_shared_codec_oversized_length_raises():
    decoder = FrameDecoder()
    decoder.feed(struct.pack(">I", MAX_FRAME_BYTES + 1))
    with pytest.raises(FrameError):
        decoder.next_frame()


def test_shared_codec_refuses_to_encode_oversized():
    with pytest.raises(FrameError):
        encode_frame(b"x" * (MAX_FRAME_BYTES + 1))


def test_script_codec_matches_shared_format(script_module):
    payload = b'{"type": "get_script_info", "params": {}}'
    framed = script_module._encode_frame(payload)
    assert framed == encode_frame(payload)
    decoder = script_module._FrameDecoder()
    for i in range(0, len(framed), 3):
        decoder.feed(framed[i:i + 3])
    assert decoder.next_frame() == payload
    decoder = script_module._FrameDecoder()
    decoder.feed(struct.pack(
        ">I", script_module.MAX_REQUEST_BYTES + 1))
    with pytest.raises(ValueError):
        decoder.next_frame()


# ------------------------------------------------------- handshake

def test_handshake_advertises_framing_v2(script_module):
    info = make_instance(script_module)._get_script_info()
    assert info["agent0_framing"] == ["v1", "v2"]
    # The v1 handshake contract is untouched: protocol stays 1.
    assert info["protocol_version"] == 1
    assert info["script_version"] == script_module.SCRIPT_VERSION == "1.7.10"


# ------------------------------------------------------- script side

class FakeClient(object):
    """Hands out prepared chunks, then EOF. Records what was sent."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.sent = []
        self.closed = False

    def settimeout(self, _timeout):
        pass

    def recv(self, _size):
        return self._chunks.pop(0) if self._chunks else b""

    def sendall(self, data):
        self.sent.append(data)

    def close(self):
        self.closed = True


def _serve(script_module, chunks, stub_impls=True):
    """Run one client session through the real _handle_client.

    Returns (received commands, raw reply chunks, client). With
    stub_impls the dispatcher is replaced by an echo; without it the
    real dispatcher runs (callers stub the Live-facing impls).
    """
    inst = make_instance(script_module)
    inst.running = True
    received = []
    if stub_impls:
        def _process(command):
            received.append(command)
            return {"status": "success",
                    "result": {"echo": command.get("type")}}
        inst._process_command = _process
    client = FakeClient(chunks)
    inst._handle_client(client)
    return inst, received, client


def _decode_frames(chunks):
    decoder = FrameDecoder()
    payloads = []
    for chunk in chunks:
        decoder.feed(chunk)
        while True:
            payload = decoder.next_frame()
            if payload is None:
                break
            payloads.append(json.loads(payload.decode("utf-8")))
    return payloads


def _framed(command):
    return encode_frame(json.dumps(command).encode("utf-8"))


def test_v2_single_frame_round_trip(script_module):
    request = _framed({"type": "get_session_info", "params": {}})
    _inst, received, client = _serve(script_module, [request])
    assert [c["type"] for c in received] == ["get_session_info"]
    replies = _decode_frames(client.sent)
    assert replies == [{"status": "success",
                        "result": {"echo": "get_session_info"}}]


def test_v2_frame_split_across_tiny_chunks(script_module):
    request = _framed({"type": "get_session_info", "params": {}})
    chunks = [request[i:i + 1] for i in range(len(request))]
    _inst, received, client = _serve(script_module, chunks)
    assert len(received) == 1
    assert len(_decode_frames(client.sent)) == 1


def test_v2_multiple_frames_coalesced_in_one_chunk(script_module):
    blob = (_framed({"type": "start_playback", "params": {}})
            + _framed({"type": "stop_playback", "params": {}}))
    _inst, received, client = _serve(script_module, [blob])
    assert [c["type"] for c in received] == ["start_playback", "stop_playback"]
    replies = _decode_frames(client.sent)
    assert [r["result"]["echo"] for r in replies] == [
        "start_playback", "stop_playback"]


def test_v2_oversized_frame_gets_a_framed_error_not_a_hang(
        script_module, monkeypatch):
    monkeypatch.setattr(script_module, "MAX_REQUEST_BYTES", 1024)
    chunks = [struct.pack(">I", 2048) + b"x" * 64]
    _inst, received, client = _serve(script_module, chunks)
    assert received == []
    replies = _decode_frames(client.sent)
    assert len(replies) == 1
    assert replies[0]["status"] == "error"
    assert "Invalid frame length" in replies[0]["message"]
    assert client.closed is True


def test_v2_max_size_frame_is_dispatched_and_answered_framed(script_module):
    """N == MAX_FRAME_BYTES exactly: the one legal length whose prefix
    (01 00 00 00) does not start with 0x00. The sniffer must still
    decide v2 - the range check is the decoder's - the command must
    dispatch, and the reply must be a frame."""
    base = json.dumps({"type": "get_session_info",
                       "params": {"pad": ""}}).encode("utf-8")
    pad = MAX_FRAME_BYTES - len(base)
    payload = json.dumps({"type": "get_session_info",
                          "params": {"pad": "x" * pad}}).encode("utf-8")
    assert len(payload) == MAX_FRAME_BYTES == 16777216
    framed = encode_frame(payload)
    assert framed[:4] == b"\x01\x00\x00\x00"
    chunks = [framed[i:i + 65536] for i in range(0, len(framed), 65536)]
    _inst, received, client = _serve(script_module, chunks)
    assert [c["type"] for c in received] == ["get_session_info"]
    assert len(received[0]["params"]["pad"]) == pad
    replies = _decode_frames(client.sent)
    assert replies == [{"status": "success",
                        "result": {"echo": "get_session_info"}}]
    assert client.closed is True


def test_v2_invalid_json_payload_gets_a_framed_error(script_module):
    _inst, received, client = _serve(script_module,
                                     [encode_frame(b"this is not json")])
    assert received == []
    replies = _decode_frames(client.sent)
    assert len(replies) == 1
    assert replies[0]["status"] == "error"
    assert client.closed is True


def test_v2_non_object_payload_gets_a_framed_error(script_module):
    _inst, received, client = _serve(script_module, [encode_frame(b"[1, 2]")])
    assert received == []
    replies = _decode_frames(client.sent)
    assert len(replies) == 1
    assert replies[0]["status"] == "error"
    assert "not a JSON object" in replies[0]["message"]


def test_garbage_prefix_closes_without_a_reply(script_module):
    _inst, received, client = _serve(
        script_module, [b"\xde\xad\xbe\xef not a protocol at all"])
    assert received == []
    assert client.sent == []          # no framing to express an error in
    assert client.closed is True


def test_v1_client_against_v2_script_is_answered_in_v1(script_module):
    payload = json.dumps(
        {"type": "get_session_info", "params": {}}).encode("utf-8")
    chunks = [payload[i:i + 3] for i in range(0, len(payload), 3)]
    _inst, received, client = _serve(script_module, chunks)
    assert len(received) == 1
    # The reply is the raw v1 object itself, not a frame.
    assert len(client.sent) == 1
    assert json.loads(client.sent[0].decode("utf-8")) == {
        "status": "success", "result": {"echo": "get_session_info"}}


def test_v1_whitespace_padded_request_still_detects_as_v1(script_module):
    payload = b"  \n" + json.dumps(
        {"type": "get_session_info", "params": {}}).encode("utf-8")
    _inst, received, client = _serve(script_module, [payload])
    assert len(received) == 1
    assert json.loads(client.sent[0].decode("utf-8"))["status"] == "success"


def test_v2_request_id_echo_and_timing_compose(script_module):
    """The frame payload's request_id is MCP-01's: echoed, with timing."""
    inst = make_instance(script_module)
    inst.running = True
    inst._get_session_info = lambda: {"tempo": 120.0}
    client = FakeClient([_framed(
        {"type": "get_session_info", "params": {}, "request_id": 42})])
    inst._handle_client(client)
    replies = _decode_frames(client.sent)
    assert len(replies) == 1
    assert replies[0]["request_id"] == 42
    assert replies[0]["result"] == {"tempo": 120.0}
    assert replies[0]["agent0_timing"]["queue_ms"] is None  # direct


def test_batch_over_v2(script_module):
    inst = make_instance(script_module)
    inst.running = True
    inst._get_session_info = lambda: {"tempo": 120.0}
    inst._set_tempo = lambda tempo: {"tempo": tempo}
    batch = {"type": "execute_batch", "params": {"commands": [
        {"type": "get_session_info"},
        {"type": "set_tempo", "params": {"tempo": 121.0}},
        {"type": "get_session_info"},
    ]}}
    client = FakeClient([_framed(batch)])
    inst._handle_client(client)
    replies = _decode_frames(client.sent)
    assert len(replies) == 1
    assert replies[0]["result"]["summary"] == {
        "total": 3, "succeeded": 3, "failed": 0, "skipped": 0}
    # The batch's one main-thread scheduling survived framing intact.
    assert len(inst.schedule_calls) == 1


# ------------------------------------------------------- server side

class FakeSockV2:
    """A socket pre-negotiated to v2: serves one framed canned response
    in small slices, records everything sent to it."""

    def __init__(self, response_obj):
        self._data = encode_frame(json.dumps(response_obj).encode("utf-8"))
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


def test_server_v2_exchange_frames_and_allocates_request_id():
    from MCP_Server.server import AbletonConnection
    timing = {"dispatch_ms": 1.0, "queue_ms": None, "execution_ms": None}
    conn = AbletonConnection("127.0.0.1", 9877)
    conn.sock = FakeSockV2({"status": "success", "result": {"tempo": 120.0},
                            "request_id": 1, "agent0_timing": timing})
    conn._framing_v2 = True  # as a v2 handshake would have set it
    result = conn.send_command("get_session_info")
    assert result == {"tempo": 120.0}
    # Exactly one send, and it is a frame whose payload carries the
    # natively allocated request_id 1.
    assert len(conn.sock.sent) == 1
    decoder = FrameDecoder()
    decoder.feed(conn.sock.sent[0])
    sent = json.loads(decoder.next_frame().decode("utf-8"))
    assert sent == {"type": "get_session_info", "params": {},
                    "request_id": 1}
    # The echo was verified and the timing captured, as in MCP-01.
    assert conn.last_request_id == 1
    assert conn.last_timing == timing
    # Next allocation on this connection is 2, never a reuse.
    conn.sock = FakeSockV2({"status": "success", "result": {},
                            "request_id": 2, "agent0_timing": timing})
    conn.send_command("get_session_info")
    assert conn.last_request_id == 2


def test_server_negotiation_requires_the_advertisement():
    from MCP_Server.server import AbletonConnection
    conn = AbletonConnection("127.0.0.1", 9877)
    conn._note_script_info({"script_version": "1.7.3"})      # no key
    assert conn._framing_v2 is False
    conn._note_script_info({"agent0_framing": ["v1"]})       # v2 absent
    assert conn._framing_v2 is False
    conn._note_script_info({"agent0_framing": ["v1", "v2"]})
    assert conn._framing_v2 is True
    conn._note_script_info({"script_version": "1.7.0"})      # downgrade
    assert conn._framing_v2 is False
    conn._note_script_info(None)
    assert conn._framing_v2 is False


V1_SCRIPT_INFO = {"name": "AbletonMCP", "script_version": "1.7.3",
                  "protocol_version": 1, "capabilities": []}
V2_SCRIPT_INFO = dict(V1_SCRIPT_INFO, script_version="1.7.10",
                      agent0_framing=["v1", "v2"])


class SniffingPeer:
    """A fake Remote Script behind a real socket.

    Sniffs each connection like the real script: the handshake is
    answered in v1 (it arrives in v1); once this peer has advertised
    v2 (script_info carries agent0_framing), later requests on the
    connection are read as frames and answered as frames. Records
    (framing, command) for every request. Can corrupt request_id
    echoes or answer with an oversized frame header.
    """

    def __init__(self, script_info, corrupt_echo=False, oversized=False):
        self.script_info = script_info
        self.corrupt_echo = corrupt_echo
        self.oversized = oversized
        self.requests = []
        self.connections = 0
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
            threading.Thread(target=self._serve, args=(client,),
                             daemon=True).start()

    def _respond(self, command):
        if command.get("type") == "get_script_info":
            result = self.script_info
        else:
            result = {"ok": True}
        response = {"status": "success", "result": result}
        if "request_id" in command:
            rid = command["request_id"] + (1 if self.corrupt_echo else 0)
            response["request_id"] = rid
            response["agent0_timing"] = {
                "dispatch_ms": 0.0, "queue_ms": None, "execution_ms": None}
        return response

    def _serve(self, client):
        framing = None
        decoder = FrameDecoder()
        buffer = b""
        try:
            while True:
                data = client.recv(65536)
                if not data:
                    return
                if framing == "v2":
                    decoder.feed(data)
                    payload = decoder.next_frame()
                    if payload is None:
                        continue
                    command = json.loads(payload.decode("utf-8"))
                else:
                    buffer += data
                    try:
                        command = json.loads(buffer.decode("utf-8"))
                    except ValueError:
                        continue
                    buffer = b""
                self.requests.append((framing or "v1", command))
                if self.oversized and framing == "v2":
                    client.sendall(struct.pack(">I", MAX_FRAME_BYTES + 1))
                    continue
                payload = json.dumps(self._respond(command)).encode()
                if framing == "v2":
                    client.sendall(encode_frame(payload))
                else:
                    client.sendall(payload)
                if (command.get("type") == "get_script_info"
                        and "v2" in (self.script_info.get(
                            "agent0_framing") or [])):
                    framing = "v2"
        except (OSError, ValueError):
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


def test_v2_server_against_v1_script_stays_on_v1():
    from MCP_Server.server import AbletonConnection
    peer = SniffingPeer(V1_SCRIPT_INFO)
    try:
        conn = AbletonConnection("127.0.0.1", peer.port)
        conn.send_command("get_script_info")
        assert conn._framing_v2 is False
        conn.send_command("get_session_info")
        conn.disconnect()
        assert [f for f, _c in peer.requests] == ["v1", "v1"]
        # No request_id was attached: v1 wire shape is byte-identical
        # to before MCP-03.
        assert all("request_id" not in c for _f, c in peer.requests)
    finally:
        peer.close()


def test_v2_server_negotiates_then_frames_with_native_ids():
    from MCP_Server.server import AbletonConnection
    peer = SniffingPeer(V2_SCRIPT_INFO)
    try:
        conn = AbletonConnection("127.0.0.1", peer.port)
        info = conn.send_command("get_script_info")
        assert info["agent0_framing"] == ["v1", "v2"]
        assert conn._framing_v2 is True
        conn.send_command("get_session_info")
        conn.send_command("get_session_info", {}, request_id=99)
        conn.disconnect()
        assert [f for f, _c in peer.requests] == ["v1", "v2", "v2"]
        # Native allocation for the plain call; the caller's id honoured.
        assert peer.requests[1][1]["request_id"] == 1
        assert peer.requests[2][1]["request_id"] == 99
        assert conn.last_request_id == 99
        assert conn.last_timing is not None
    finally:
        peer.close()


def test_v2_echo_mismatch_drops_the_connection():
    from MCP_Server.server import AbletonConnection
    peer = SniffingPeer(V2_SCRIPT_INFO, corrupt_echo=True)
    try:
        conn = AbletonConnection("127.0.0.1", peer.port)
        conn.send_command("get_script_info")
        with pytest.raises(Exception):
            conn.send_command("get_session_info")
        assert conn.sock is None
    finally:
        peer.close()


def test_v2_oversized_response_frame_drops_the_connection():
    from MCP_Server.server import AbletonConnection
    peer = SniffingPeer(V2_SCRIPT_INFO, oversized=True)
    try:
        conn = AbletonConnection("127.0.0.1", peer.port)
        conn.send_command("get_script_info")
        with pytest.raises(Exception):
            conn.send_command("get_session_info")
        assert conn.sock is None
    finally:
        peer.close()


# ------------------------------------------------------- end to end

class ScriptPeer:
    """The real Remote Script behind a real socket (as in MCP-02's
    tests), counting top-level wire requests by recursion depth."""

    def __init__(self, script_module):
        inst = make_instance(script_module)
        inst.running = True
        inst._get_session_info = lambda: {"tempo": 120.0}
        inst._set_tempo = lambda tempo: {"tempo": tempo}
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


def test_end_to_end_negotiation_and_batch_over_v2(script_module):
    """Real server <-> real script: handshake in v1, everything after
    it framed - including one execute_batch as exactly one more wire
    request."""
    from MCP_Server.server import AbletonConnection
    peer = ScriptPeer(script_module)
    try:
        conn = AbletonConnection("127.0.0.1", peer.port)
        info = conn.send_command("get_script_info")
        assert info["agent0_framing"] == ["v1", "v2"]
        assert conn._framing_v2 is True
        assert conn.send_command("get_session_info") == {"tempo": 120.0}
        result = conn.send_batch([
            {"type": "get_session_info"},
            {"type": "set_tempo", "params": {"tempo": 121.0}},
            {"type": "get_session_info"},
        ])
        conn.disconnect()
        assert result["summary"] == {"total": 3, "succeeded": 3,
                                     "failed": 0, "skipped": 0}
        # handshake + one direct call + ONE batch exchange.
        assert peer.requests == 3
        assert peer.connections == 1
        # Every framed exchange carried an id and captured timing.
        assert conn.last_timing is not None
    finally:
        peer.close()
