"""MCP-09 tests: connection lifecycle and timeout correctness.

Before MCP-09 the server's timeout/connection handling had three
defects, all pinned here:

* the v1 receive path forced ``sock.settimeout(15.0)``, silently
  capping every per-command budget - create_audio_clip's 65 s and
  execute_batch's summed budget included;
* error paths assigned ``self.sock = None`` without ever closing the
  socket, and left the negotiated framing flag set;
* every failure was a plain ``Exception``, so a command that timed out
  AFTER being sent (and may well have been applied in Live) looked
  exactly like one that never left the server.

These tests pin the fixed behaviour: budgets are selected per command,
applied before the send, and honoured by both receive paths; every
error path closes the socket and resets per-connection state through
the one teardown path; post-send failures raise OutcomeUnknownError
while pre-send failures and definitive answers keep raising plain
exceptions.

GapSock simulates the peer's response gap virtually: its recv raises
socket.timeout iff the scripted gap exceeds the timeout in force -
exactly the contract a real socket offers - so a 20 s gap can be
tested against 10/15/25/65 s budgets without a real 20 s wait. The
RealPeer tests at the end cover the real socket timeout machinery
with short real delays and instance-patched budgets.
"""

import json
import socket
import threading
import time

import pytest

from MCP_Server.framing import encode_frame
from MCP_Server.server import AbletonConnection, OutcomeUnknownError


# ------------------------------------------------------- fake sockets

class GapSock:
    """Canned-response socket with a simulated response gap.

    Serves ``payload`` in 5-byte slices. The first recv() applies the
    gap: if the timeout most recently set on the socket is smaller
    than the gap, it raises socket.timeout (what a real socket does
    when no data arrives in time); otherwise the data flows. ``gap``
    is virtual - nothing actually sleeps.
    """

    def __init__(self, payload=b"", gap=0.0, send_error=None,
                 mid_error=None, mid_after=None):
        self._data = payload
        self._i = 0
        self.gap = gap
        self.send_error = send_error
        self.mid_error = mid_error
        self.mid_after = mid_after
        self.sent = []
        self.timeouts = []
        self.closed = False
        self._timeout = None
        self._gap_done = False

    def settimeout(self, timeout):
        self.timeouts.append(timeout)
        self._timeout = timeout

    def sendall(self, data):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(data)

    def recv(self, _size):
        if not self._gap_done:
            self._gap_done = True
            if self._timeout is not None and self.gap > self._timeout:
                raise socket.timeout("timed out (simulated gap)")
        if (self.mid_error is not None and self.mid_after is not None
                and self._i >= self.mid_after):
            raise self.mid_error
        if self._i >= len(self._data):
            return b""
        chunk = self._data[self._i:self._i + 5]
        self._i += len(chunk)
        return chunk

    def close(self):
        self.closed = True


def _conn(sock=None, framing_v2=False):
    conn = AbletonConnection("127.0.0.1", 9877)
    if sock is not None:
        conn.sock = sock
    conn._framing_v2 = framing_v2
    return conn


def _v1_payload(result):
    return json.dumps({"status": "success", "result": result}).encode()


def _v2_payload(result, request_id=1):
    return encode_frame(json.dumps(
        {"status": "success", "result": result,
         "request_id": request_id}).encode())


# ------------------------------------------------------- budget table

def test_command_timeout_table():
    conn = _conn()
    assert conn._command_timeout("create_audio_clip") == 65.0
    assert conn._command_timeout("set_tempo") == 15.0        # modifying
    assert conn._command_timeout("get_session_info") == 10.0  # read
    # Batch: sum of the per-item budgets, floored at 15.
    assert conn._command_timeout("execute_batch", {"commands": []}) == 15.0
    assert conn._command_timeout(
        "execute_batch",
        {"commands": [{"type": "create_audio_clip"}]}) == 65.0
    assert conn._command_timeout(
        "execute_batch",
        {"commands": [{"type": "set_tempo"},
                      {"type": "get_session_info"}]}) == 25.0
    assert conn._command_timeout("execute_batch", None) == 15.0


def test_receive_full_response_no_longer_sets_a_timeout():
    # The unit-level pin for the removed override: the v1 receive path
    # must not call settimeout at all; the caller's budget stands.
    conn = _conn()
    sock = GapSock(_v1_payload({"tempo": 120.0}))
    data = conn.receive_full_response(sock)
    assert json.loads(data.decode())["result"] == {"tempo": 120.0}
    assert sock.timeouts == []


# ------------------------------------------- budgets honoured (v1+v2)

def test_v1_long_command_survives_a_gap_over_15s():
    # Simulated 20 s response gap: fatal under the old hard 15 s v1
    # override, fine under create_audio_clip's real 65 s budget.
    sock = GapSock(_v1_payload({"clip_name": "x"}), gap=20.0)
    conn = _conn(sock)
    result = conn.send_command("create_audio_clip", {"track_index": 0})
    assert result == {"clip_name": "x"}
    # Exactly one timeout is set for the whole exchange: the budget.
    assert sock.timeouts == [65.0]


def test_v2_long_command_survives_a_gap_over_15s():
    sock = GapSock(_v2_payload({"clip_name": "x"}), gap=20.0)
    conn = _conn(sock, framing_v2=True)
    result = conn.send_command("create_audio_clip", {"track_index": 0})
    assert result == {"clip_name": "x"}
    assert sock.timeouts == [65.0]


def test_v1_read_command_gap_beyond_its_budget_is_outcome_unknown():
    sock = GapSock(_v1_payload({}), gap=20.0)  # read budget is 10 s
    conn = _conn(sock)
    with pytest.raises(OutcomeUnknownError) as excinfo:
        conn.send_command("get_session_info")
    assert excinfo.value.command_type == "get_session_info"
    assert "outcome unknown" in str(excinfo.value)
    assert sock.timeouts == [10.0]
    assert sock.closed is True
    assert conn.sock is None


def test_v2_timeout_is_outcome_unknown_and_resets_state():
    sock = GapSock(_v2_payload({}), gap=20.0)
    conn = _conn(sock, framing_v2=True)
    with pytest.raises(OutcomeUnknownError):
        conn.send_command("get_session_info")
    assert sock.closed is True
    assert conn.sock is None
    # The framing adoption must not survive its connection.
    assert conn._framing_v2 is False


def test_batch_timeout_is_the_sum_and_is_honoured_on_v1():
    batch_result = {"results": [], "summary": {"total": 2, "succeeded": 2,
                                               "failed": 0, "skipped": 0}}
    sock = GapSock(_v1_payload(batch_result), gap=20.0)
    conn = _conn(sock)
    result = conn.send_batch([{"type": "set_tempo", "params": {"tempo": 1}},
                              {"type": "get_session_info"}])
    assert result == batch_result
    assert sock.timeouts == [25.0]  # 15 + 10, and > the old 15 s cap


def test_batch_post_send_failure_is_outcome_unknown_at_batch_level():
    sock = GapSock(_v1_payload({}), gap=100.0)
    conn = _conn(sock)
    with pytest.raises(OutcomeUnknownError) as excinfo:
        conn.send_batch([{"type": "set_tempo", "params": {"tempo": 1}}])
    assert excinfo.value.command_type == "execute_batch"
    assert "per-item outcomes are unknowable" in str(excinfo.value)
    assert sock.closed is True
    assert conn.sock is None


# --------------------------------------- pre-send failures stay plain

def test_connect_refused_is_not_outcome_unknown():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()  # nothing listens on this port now
    conn = AbletonConnection("127.0.0.1", port)
    with pytest.raises(ConnectionError) as excinfo:
        conn.send_command("get_session_info")
    assert not isinstance(excinfo.value, OutcomeUnknownError)


def test_send_failure_is_not_outcome_unknown_and_closes_socket():
    sock = GapSock(_v1_payload({}), send_error=BrokenPipeError("boom"))
    conn = _conn(sock)
    with pytest.raises(Exception) as excinfo:
        conn.send_command("set_tempo", {"tempo": 120.0})
    assert not isinstance(excinfo.value, OutcomeUnknownError)
    assert "Connection to Ableton lost" in str(excinfo.value)
    assert sock.closed is True
    assert conn.sock is None


def test_script_error_envelope_is_definitive_not_outcome_unknown():
    payload = json.dumps({"status": "error",
                          "message": "Unknown command: nope"}).encode()
    sock = GapSock(payload)
    conn = _conn(sock)
    with pytest.raises(Exception) as excinfo:
        conn.send_command("nope")
    assert not isinstance(excinfo.value, OutcomeUnknownError)
    assert "Unknown command: nope" in str(excinfo.value)
    # The connection is still dropped (pre-MCP-09 behaviour), but now
    # the socket is actually closed.
    assert sock.closed is True
    assert conn.sock is None


# --------------------------------- post-send failure shapes (v1 + v2)

def test_v1_reset_mid_response_is_outcome_unknown_and_closes():
    sock = GapSock(_v1_payload({"tempo": 120.0}),
                   mid_error=ConnectionResetError("reset by peer"),
                   mid_after=10)
    conn = _conn(sock)
    with pytest.raises(OutcomeUnknownError):
        conn.send_command("get_session_info")
    assert sock.closed is True
    assert conn.sock is None


def test_v1_eof_mid_response_is_outcome_unknown_and_closes():
    sock = GapSock(_v1_payload({"tempo": 120.0})[:20])  # truncated JSON
    conn = _conn(sock)
    with pytest.raises(OutcomeUnknownError):
        conn.send_command("get_session_info")
    assert sock.closed is True
    assert conn.sock is None


def test_v1_eof_before_any_data_is_outcome_unknown_and_closes():
    sock = GapSock(b"")
    conn = _conn(sock)
    with pytest.raises(OutcomeUnknownError):
        conn.send_command("get_session_info")
    assert sock.closed is True
    assert conn.sock is None


def test_v2_mid_frame_eof_is_outcome_unknown_and_closes():
    framed = _v2_payload({"tempo": 120.0})
    sock = GapSock(framed[:10])  # header + partial payload, then EOF
    conn = _conn(sock, framing_v2=True)
    with pytest.raises(OutcomeUnknownError):
        conn.send_command("get_session_info")
    assert sock.closed is True
    assert conn.sock is None
    assert conn._framing_v2 is False


def test_v2_unparseable_response_is_outcome_unknown_and_closes():
    sock = GapSock(encode_frame(b"this is not json at all"))
    conn = _conn(sock, framing_v2=True)
    with pytest.raises(OutcomeUnknownError) as excinfo:
        conn.send_command("get_session_info")
    assert "Invalid response from Ableton" in str(excinfo.value)
    assert sock.closed is True
    assert conn.sock is None


def test_v2_non_utf8_response_is_outcome_unknown_and_closes():
    # A complete framed response whose bytes are not valid UTF-8:
    # decode raises UnicodeDecodeError, not JSONDecodeError, and it
    # must classify exactly like a parse failure (MCP-09 review F1) -
    # the command may have been applied, so it is not safe to retry.
    sock = GapSock(encode_frame(b"\xff\xfe\x00not utf8"))
    conn = _conn(sock, framing_v2=True)
    with pytest.raises(OutcomeUnknownError) as excinfo:
        conn.send_command("get_session_info")
    assert "Invalid response from Ableton" in str(excinfo.value)
    assert sock.closed is True
    assert conn.sock is None


def test_v1_non_utf8_response_is_outcome_unknown_and_closes():
    # The same bytes on the v1 path: the completeness probe inside
    # receive_full_response cannot decode them either, and the
    # failure must still surface as outcome-unknown, not a plain
    # "safe to retry" error.
    sock = GapSock(b"\xff\xfe\x00not utf8")
    conn = _conn(sock)
    with pytest.raises(OutcomeUnknownError):
        conn.send_command("get_session_info")
    assert sock.closed is True
    assert conn.sock is None


def test_framing_reset_means_the_next_exchange_starts_on_v1():
    bad = GapSock(_v2_payload({}), gap=20.0)
    conn = _conn(bad, framing_v2=True)
    with pytest.raises(OutcomeUnknownError):
        conn.send_command("get_session_info")
    # A fresh socket on the same connection object: the request must go
    # out as raw v1 JSON (a framed request would not parse as JSON).
    good = GapSock(_v1_payload({"tempo": 121.0}))
    conn.sock = good
    assert conn.send_command("get_session_info") == {"tempo": 121.0}
    sent = json.loads(good.sent[0].decode("utf-8"))
    assert sent["type"] == "get_session_info"
    assert conn._framing_v2 is False


# ------------------------------------------------------- connect/disconnect

def test_connect_failure_closes_the_created_socket(monkeypatch):
    import MCP_Server.server as server

    created = []

    class RefusingSock:
        def __init__(self, *args, **kwargs):
            self.closed = False
            created.append(self)

        def settimeout(self, _timeout):
            pass

        def connect(self, _addr):
            raise ConnectionRefusedError("refused")

        def close(self):
            self.closed = True

    monkeypatch.setattr(server.socket, "socket", RefusingSock)
    conn = server.AbletonConnection("127.0.0.1", 9877)
    assert conn.connect() is False
    assert len(created) == 1
    assert created[0].closed is True
    assert conn.sock is None


def test_disconnect_closes_and_resets():
    sock = GapSock(_v1_payload({}))
    conn = _conn(sock, framing_v2=True)
    conn.disconnect()
    assert sock.closed is True
    assert conn.sock is None
    assert conn._framing_v2 is False


# ------------------------------------------------------- real sockets

class RealPeer:
    """One end of a real socketpair; answers after a real delay.

    The request is read first (one sendall from the client), then the
    thread sleeps ``delay`` and sends the prepared response - a real
    response gap through the real socket timeout machinery.
    """

    def __init__(self, payload, delay):
        self.client, self.peer = socket.socketpair()
        self._payload = payload
        self._delay = delay
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        try:
            self.peer.settimeout(5.0)
            self.peer.recv(65536)
            time.sleep(self._delay)
            self.peer.sendall(self._payload)
        except Exception:
            pass  # the client may already be gone (timeout tests)

    def start(self):
        self.thread.start()
        return self.client


def test_real_socket_delay_within_budget_completes():
    peer = RealPeer(_v1_payload({"tempo": 122.0}), delay=0.4)
    conn = _conn(peer.start())
    conn._command_timeout = lambda command_type, params=None: 5.0
    started = time.perf_counter()
    assert conn.send_command("get_session_info") == {"tempo": 122.0}
    assert time.perf_counter() - started >= 0.4  # it really waited


def test_real_socket_delay_beyond_budget_is_outcome_unknown():
    peer = RealPeer(_v1_payload({"tempo": 122.0}), delay=1.0)
    conn = _conn(peer.start())
    conn._command_timeout = lambda command_type, params=None: 0.15
    started = time.perf_counter()
    with pytest.raises(OutcomeUnknownError):
        conn.send_command("get_session_info")
    # Gave up on the budget, not on the peer's 1 s delay.
    assert time.perf_counter() - started < 1.0
    assert conn.sock is None


# ------------------------------------------- SI-73 envelope validation
#
# SI-70 F3: a complete, valid-JSON but non-envelope response was
# accepted or crashed outside the MCP-09 classification. A non-object
# reply raised a plain "Communication error ... 'list' object has no
# attribute 'get'" (readable as definitive/safe-to-retry); a dict with
# a missing or unrecognised status was returned as success; a success
# envelope with no result silently returned {}. The pins below lock
# the fixed classification: after the request was fully sent, a
# non-envelope reply is OutcomeUnknownError (the command may have
# been applied), the socket is dropped through the one teardown
# path, and well-formed envelopes behave byte-identically to before.
#
# Decision (SI-73): {"status": "success"} with no "result" key is a
# non-envelope and is outcome-unknown, NOT a silent {}. The real
# script's success envelope always carries a result key (its
# dispatcher initialises "result": {} and every success reply sets
# it), so a missing key means the peer's answer is not a script
# envelope; returning {} would fabricate a result the peer never
# sent. A result key that is present - {} or null included - is a
# well-formed success and is returned exactly as before (controls).

def _assert_envelope_outcome_unknown(excinfo, sock, conn, command_type="get_session_info"):
    assert excinfo.value.command_type == command_type
    assert "Invalid response from Ableton" in str(excinfo.value)
    assert "outcome unknown" in str(excinfo.value)
    assert sock.closed is True
    assert conn.sock is None


def test_v1_non_object_list_response_is_outcome_unknown_and_closes():
    sock = GapSock(json.dumps([1, 2, 3]).encode())
    conn = _conn(sock)
    with pytest.raises(OutcomeUnknownError) as excinfo:
        conn.send_command("get_session_info")
    assert "not a script envelope" in str(excinfo.value)
    assert "got list" in str(excinfo.value)
    _assert_envelope_outcome_unknown(excinfo, sock, conn)


def test_v1_non_object_number_response_is_outcome_unknown_and_closes():
    sock = GapSock(json.dumps(42).encode())
    conn = _conn(sock)
    with pytest.raises(OutcomeUnknownError) as excinfo:
        conn.send_command("get_session_info")
    assert "not a script envelope" in str(excinfo.value)
    assert "got int" in str(excinfo.value)
    _assert_envelope_outcome_unknown(excinfo, sock, conn)


def test_v1_non_object_string_response_is_outcome_unknown_and_closes():
    sock = GapSock(json.dumps("text").encode())
    conn = _conn(sock)
    with pytest.raises(OutcomeUnknownError) as excinfo:
        conn.send_command("get_session_info")
    assert "not a script envelope" in str(excinfo.value)
    assert "got str" in str(excinfo.value)
    _assert_envelope_outcome_unknown(excinfo, sock, conn)


def test_v1_non_object_bool_response_is_outcome_unknown_and_closes():
    sock = GapSock(json.dumps(True).encode())
    conn = _conn(sock)
    with pytest.raises(OutcomeUnknownError) as excinfo:
        conn.send_command("get_session_info")
    assert "not a script envelope" in str(excinfo.value)
    assert "got bool" in str(excinfo.value)
    _assert_envelope_outcome_unknown(excinfo, sock, conn)


def test_v1_null_response_is_outcome_unknown_and_closes():
    sock = GapSock(json.dumps(None).encode())
    conn = _conn(sock)
    with pytest.raises(OutcomeUnknownError) as excinfo:
        conn.send_command("get_session_info")
    assert "not a script envelope" in str(excinfo.value)
    assert "got NoneType" in str(excinfo.value)
    _assert_envelope_outcome_unknown(excinfo, sock, conn)


def test_v1_missing_status_response_is_outcome_unknown_and_closes():
    sock = GapSock(json.dumps({"result": {"x": 1}}).encode())
    conn = _conn(sock)
    with pytest.raises(OutcomeUnknownError) as excinfo:
        conn.send_command("get_session_info")
    assert "status is missing or unrecognised (None)" in str(excinfo.value)
    _assert_envelope_outcome_unknown(excinfo, sock, conn)


def test_v1_unrecognised_status_response_is_outcome_unknown_and_closes():
    sock = GapSock(json.dumps(
        {"status": "pending", "result": {"x": 2}}).encode())
    conn = _conn(sock)
    with pytest.raises(OutcomeUnknownError) as excinfo:
        conn.send_command("get_session_info")
    assert "status is missing or unrecognised ('pending')" in str(excinfo.value)
    _assert_envelope_outcome_unknown(excinfo, sock, conn)


def test_v1_empty_envelope_response_is_outcome_unknown_and_closes():
    sock = GapSock(json.dumps({}).encode())
    conn = _conn(sock)
    with pytest.raises(OutcomeUnknownError) as excinfo:
        conn.send_command("get_session_info")
    assert "status is missing or unrecognised (None)" in str(excinfo.value)
    _assert_envelope_outcome_unknown(excinfo, sock, conn)


def test_v1_success_without_result_is_outcome_unknown_and_closes():
    sock = GapSock(json.dumps({"status": "success"}).encode())
    conn = _conn(sock)
    with pytest.raises(OutcomeUnknownError) as excinfo:
        conn.send_command("get_session_info")
    assert "success envelope carries no 'result'" in str(excinfo.value)
    _assert_envelope_outcome_unknown(excinfo, sock, conn)


def test_v2_non_object_response_is_outcome_unknown_and_closes():
    sock = GapSock(encode_frame(json.dumps([1, 2, 3]).encode()))
    conn = _conn(sock, framing_v2=True)
    with pytest.raises(OutcomeUnknownError) as excinfo:
        conn.send_command("get_session_info")
    assert "not a script envelope" in str(excinfo.value)
    _assert_envelope_outcome_unknown(excinfo, sock, conn)
    assert conn._framing_v2 is False


def test_v2_missing_status_response_is_outcome_unknown_and_closes():
    sock = GapSock(encode_frame(json.dumps(
        {"result": {"x": 1}, "request_id": 1}).encode()))
    conn = _conn(sock, framing_v2=True)
    with pytest.raises(OutcomeUnknownError) as excinfo:
        conn.send_command("get_session_info")
    assert "status is missing or unrecognised (None)" in str(excinfo.value)
    _assert_envelope_outcome_unknown(excinfo, sock, conn)
    assert conn._framing_v2 is False


def test_v2_unrecognised_status_response_is_outcome_unknown_and_closes():
    sock = GapSock(encode_frame(json.dumps(
        {"status": "pending", "result": {"x": 2},
         "request_id": 1}).encode()))
    conn = _conn(sock, framing_v2=True)
    with pytest.raises(OutcomeUnknownError) as excinfo:
        conn.send_command("get_session_info")
    assert "status is missing or unrecognised ('pending')" in str(excinfo.value)
    _assert_envelope_outcome_unknown(excinfo, sock, conn)
    assert conn._framing_v2 is False


def test_v2_success_without_result_is_outcome_unknown_and_closes():
    sock = GapSock(encode_frame(json.dumps(
        {"status": "success", "request_id": 1}).encode()))
    conn = _conn(sock, framing_v2=True)
    with pytest.raises(OutcomeUnknownError) as excinfo:
        conn.send_command("get_session_info")
    assert "success envelope carries no 'result'" in str(excinfo.value)
    _assert_envelope_outcome_unknown(excinfo, sock, conn)
    assert conn._framing_v2 is False


def test_batch_non_envelope_response_is_outcome_unknown_at_batch_level():
    sock = GapSock(json.dumps([1, 2, 3]).encode())
    conn = _conn(sock)
    with pytest.raises(OutcomeUnknownError) as excinfo:
        conn.send_batch([{"type": "set_tempo", "params": {"tempo": 1}}])
    assert "per-item outcomes are unknowable" in str(excinfo.value)
    _assert_envelope_outcome_unknown(
        excinfo, sock, conn, command_type="execute_batch")


# SI-73 controls: well-formed envelopes are byte-identical to base.

def test_control_success_with_empty_result_stays_success_and_open():
    sock = GapSock(_v1_payload({}))
    conn = _conn(sock)
    assert conn.send_command("get_session_info") == {}
    assert sock.closed is False
    assert conn.sock is sock


def test_control_success_with_null_result_stays_success_and_open():
    payload = json.dumps({"status": "success", "result": None}).encode()
    sock = GapSock(payload)
    conn = _conn(sock)
    assert conn.send_command("get_session_info") is None
    assert sock.closed is False
    assert conn.sock is sock


def test_control_error_envelope_without_result_stays_plain():
    # Pre-dispatch script errors carry no "result" key by design;
    # that shape is a definitive envelope and must not be
    # reclassified - unlike a success envelope without a result.
    payload = json.dumps({"status": "error",
                          "message": "Unknown command: nope"}).encode()
    sock = GapSock(payload)
    conn = _conn(sock)
    with pytest.raises(Exception) as excinfo:
        conn.send_command("nope")
    assert type(excinfo.value) is Exception
    assert not isinstance(excinfo.value, OutcomeUnknownError)
    assert str(excinfo.value) == (
        "Communication error with Ableton: Unknown command: nope")
    assert sock.closed is True
    assert conn.sock is None


def test_control_request_id_mismatch_on_well_formed_envelope_stays_plain():
    payload = json.dumps({"status": "success", "result": {},
                          "request_id": 999}).encode()
    sock = GapSock(payload)
    conn = _conn(sock)
    with pytest.raises(Exception) as excinfo:
        conn.send_command("get_session_info", request_id=7)
    assert type(excinfo.value) is Exception
    assert not isinstance(excinfo.value, OutcomeUnknownError)
    assert str(excinfo.value) == (
        "Communication error with Ableton: Remote Script did not "
        "echo request_id 7 (response carried 999); the loaded script "
        "may not support request IDs")
    assert sock.closed is True
    assert conn.sock is None
