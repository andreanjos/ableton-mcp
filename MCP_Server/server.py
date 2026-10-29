# ableton_mcp_server.py
from mcp.server.fastmcp import FastMCP, Context
import socket
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Any, List, Optional, Union

from .framing import FrameDecoder, FrameError, MAX_FRAME_BYTES, encode_frame


def _env_flag(name: str) -> bool:
    """Read a boolean env switch — same spellings as the ABLETON_MCP_DISABLE_* flags."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


# MCP-04: the lean profile, opt-in via ABLETON_MCP_LEAN_PROFILE=1.
#
# For deployments that drive Live purely as a control path (the Omnicorp
# profile) and never use the dataset side plane. Where the
# ABLETON_MCP_DISABLE_* switches merely gate the telemetry/dataset
# machinery off, the lean profile leaves it unwired: the side-plane
# modules are not imported at startup, and nothing on the tool-call
# control path imports them — the tool decorators are pass-throughs, the
# passive poller cannot start, and no Supabase client is constructed.
# The one exception is set_dataset_consent below: persisting the user's
# answer imports dataset.consent, which loads the side-plane modules
# without activating any recording behaviour. Tool results switch from
# pretty-printed JSON to compact JSON, and
# INFO logs carry command names and byte counts instead of full
# parameters. With the variable unset the default profile is unchanged
# in every observable way.
LEAN_PROFILE = _env_flag("ABLETON_MCP_LEAN_PROFILE")

if LEAN_PROFILE:
    def _passthrough_decorator_factory(*_args, **_kwargs):
        """Stand-in for the telemetry/trajectory decorator factories: identity."""

        def decorator(func):
            return func

        return decorator

    telemetry_tool = _passthrough_decorator_factory
    rich_telemetry_tool = _passthrough_decorator_factory
    trajectory_tool = _passthrough_decorator_factory

    def record_startup(*_args, **_kwargs):
        return None

    def dataset_enabled() -> bool:
        return False

    def get_recorder(*_args, **_kwargs):
        return None
else:
    from .telemetry import record_startup
    from .telemetry_decorator import telemetry_tool, rich_telemetry_tool
    from .dataset import dataset_enabled, get_recorder
    from .dataset.trajectory_decorator import trajectory_tool

ABLETON_HOST = os.environ.get("ABLETON_HOST", "localhost")
ABLETON_PORT = int(os.environ.get("ABLETON_PORT", "9877"))

# Configure logging
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("AbletonMCPServer")


class OutcomeUnknownError(Exception):
    """A command was fully sent but its outcome is unknown (MCP-09).

    Raised by ``AbletonConnection.send_command`` / ``send_batch`` when
    the request bytes were sent but no complete, parseable response
    came back: the wait timed out, the connection dropped or reset
    mid-response, the peer closed early, or the response bytes could
    not be framed or parsed. The Remote Script may have executed the
    command - for a state-modifying command its effect may already be
    in the session - so callers must NOT blindly retry; reconcile
    first (e.g. re-read the state the command would have changed).

    Failures before the request was fully sent (connect refused, a
    send error) and definitive answers (a script ``status: "error"``
    envelope, a request-id echo mismatch) raise plain exceptions, as
    before - never this type. It subclasses ``Exception`` directly, so
    existing ``except Exception`` handling is unchanged; the type
    itself (and the ``command_type`` attribute) is the marker callers
    branch on.
    """

    def __init__(self, message, command_type=None):
        super().__init__(message)
        self.command_type = command_type


# Wire commands that mutate session state. Used only to size socket
# timeouts (dataset classification keeps its own lists).
_MODIFYING_COMMANDS = [
    "create_midi_track", "create_audio_track", "set_track_name",
    "create_clip", "create_audio_clip", "add_notes_to_clip", "set_clip_name",
    "delete_clip",
    "clear_notes_from_clip",
    "set_tempo", "fire_clip", "stop_clip", "set_device_parameter",
    "set_device_parameters",
    "start_playback", "stop_playback", "load_instrument_or_effect",
    # Arrangement view commands
    "switch_to_arrangement_view", "set_current_song_time",
    "duplicate_session_clip_to_arrangement",
    "create_locator"
]

# Commands whose work on Live's main thread can take noticeably longer
# than the default modifying-command budget (e.g. importing/decoding a
# large audio file): command type -> socket budget in seconds.
_LONG_RUNNING_COMMAND_TIMEOUTS = {"create_audio_clip": 65.0}


#: SI-345: ceiling for a v1 (unframed) response, mirroring the Remote
#: Script's 16 MiB request cap (MAX_REQUEST_BYTES) - which is also the
#: framing-v2 payload ceiling, so the bound is the same 16 MiB in both
#: framings. The v1 receive loop accumulates chunks in memory; before
#: SI-345 nothing bounded that accumulation, so a peer trickling (or
#: flooding) a never-completing v1 response could grow it without
#: limit as well as holding the exchange past its budget.
MAX_V1_RESPONSE_BYTES = MAX_FRAME_BYTES


def _apply_exchange_deadline(sock, deadline) -> None:
    """Narrow *sock*'s timeout to the time left before *deadline*.

    SI-345: the per-command budget is an absolute monotonic deadline
    for the whole exchange (computed once by _send_command_locked,
    following the dataset recorder's flush pattern), not a fresh
    budget per recv() call. Before each receive, the socket timeout
    is narrowed to the remaining time, so no single recv() can run
    past the deadline and a peer that keeps delivering a byte inside
    every window cannot extend the exchange beyond it. Raises
    socket.timeout when the deadline has already passed, so callers
    see exactly the exception a per-recv timeout would have produced
    and the timeout classification in _send_command_locked is
    unchanged. A None deadline leaves the socket untouched: direct
    callers of the receive paths keep the pre-SI-345 contract (the
    timeout the caller set stands, unmodified). The timeout is only
    ever narrowed here, never widened back toward the full budget.
    """
    if deadline is None:
        return
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise socket.timeout("timed out (exchange deadline exceeded)")
    sock.settimeout(remaining)


def _resolve_exchange_deadline(sock, deadline):
    """Return the exchange deadline in force for a receive path.

    An explicit ``deadline`` (the production path: _send_command_locked
    computes it once from the budget, before the send) wins. Without
    one, a timeout already set on the socket is taken as the caller's
    stated budget for the exchange and converted, here at receive
    entry, into the same kind of absolute deadline - so the older
    calling convention (settimeout, then call a receive path
    directly) is bounded against a trickling peer too, instead of
    silently keeping the per-recv semantics SI-345 removed. Sockets
    with no timeout set (blocking mode) or no gettimeout (the test
    fakes) yield None: no deadline, the pre-SI-345 contract.
    """
    if deadline is not None:
        return deadline
    gettimeout = getattr(sock, "gettimeout", None)
    if not callable(gettimeout):
        return None
    current = gettimeout()
    if current is None:
        return None
    return time.monotonic() + current


@dataclass
class AbletonConnection:
    host: str
    port: int
    sock: socket.socket = None

    # MCP-01: populated only by exchanges sent with request_id=...
    # (see send_command). None until the first timed exchange completes.
    last_timing: Any = None
    last_request_id: Any = None

    # MCP-03: framing negotiated for THIS connection. False = wire v1
    # (raw JSON, byte-identical to the historical protocol). Flips True
    # only after a get_script_info result received on this connection
    # advertises framing v2 (see _note_script_info); reset by connect()
    # and disconnect(), so a fresh connection always starts at v1 and
    # re-negotiates from the handshake.
    _framing_v2: bool = False
    _next_request_id: int = 1

    # One socket, several threads: tool calls run on the MCP worker while the
    # passive poller drains events on its own thread. Serialise so their
    # sendall/recv pairs cannot interleave.
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def connect(self) -> bool:
        """Connect to the Ableton Remote Script socket server"""
        if self.sock:
            return True

        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5.0)
            sock.connect((self.host, self.port))
            sock.settimeout(None)
        except Exception as e:
            logger.error(f"Failed to connect to Ableton at {self.host}:{self.port}: {str(e)}")
            if sock is not None:
                # MCP-09: never abandon a created socket unclosed.
                try:
                    sock.close()
                except Exception:
                    pass
            self.sock = None
            return False
        self.sock = sock
        self._framing_v2 = False
        self._next_request_id = 1
        logger.info(f"Connected to Ableton at {self.host}:{self.port}")
        return True
    
    def disconnect(self):
        """Disconnect from the Ableton Remote Script"""
        with self._lock:
            self._drop_connection()

    def _drop_connection(self):
        """Close the socket (best effort) and reset per-connection state.

        The single teardown path (MCP-09): disconnect() and every
        send_command error path come through here, so a failed exchange
        never leaves its socket open behind it, and the framing
        adoption never survives the connection it was negotiated on.
        Before MCP-09 the error paths simply assigned ``self.sock =
        None``: the socket was dropped without being closed - when its
        descriptor actually closed was left to refcounting - and
        ``_framing_v2`` stayed set until the next successful connect()
        happened to reset it.
        ``_next_request_id`` is deliberately left alone: connect()
        restarts allocation at 1, as before.
        """
        sock, self.sock = self.sock, None
        self._framing_v2 = False
        if sock is not None:
            try:
                sock.close()
            except Exception as e:
                logger.error(f"Error disconnecting from Ableton: {str(e)}")


    def receive_full_response(self, sock, buffer_size=8192, deadline=None):
        """Receive the complete v1 response, potentially in chunks.

        The caller sets the socket timeout (the per-command budget,
        see _command_timeout) before the exchange and, since SI-345,
        passes the exchange's absolute monotonic ``deadline``: before
        each recv() this path narrows the socket timeout to the time
        remaining (see _apply_exchange_deadline), so the budget
        governs the WHOLE exchange rather than each recv() in
        isolation - a peer delivering a byte inside every per-recv
        window can no longer hold the exchange past the budget. It
        never widens the timeout and never substitutes a budget of
        its own. Before MCP-09 it forced ``sock.settimeout(15.0)``
        here, silently capping every v1 exchange - including
        create_audio_clip's 65 s budget and execute_batch's summed
        budget. A socket.timeout mid-receive stops the wait; partial
        bytes are then salvaged if they parse as a complete response,
        otherwise an exception is raised for the caller to classify.

        SI-345 also bounds the accumulation itself: a response that
        grows past MAX_V1_RESPONSE_BYTES (16 MiB, mirroring the
        script's request cap) is refused instead of being buffered
        without limit.
        """
        chunks = []
        total_bytes = 0
        deadline = _resolve_exchange_deadline(sock, deadline)

        try:
            while True:
                try:
                    _apply_exchange_deadline(sock, deadline)
                    chunk = sock.recv(buffer_size)
                    if not chunk:
                        if not chunks:
                            raise Exception("Connection closed before receiving any data")
                        break

                    chunks.append(chunk)
                    total_bytes += len(chunk)
                    if total_bytes > MAX_V1_RESPONSE_BYTES:
                        raise Exception(
                            "Response exceeded %d bytes without a "
                            "complete JSON response"
                            % MAX_V1_RESPONSE_BYTES)

                    # Check if we've received a complete JSON object
                    try:
                        data = b''.join(chunks)
                        json.loads(data.decode('utf-8'))
                        logger.info(f"Received complete response ({len(data)} bytes)")
                        return data
                    except json.JSONDecodeError:
                        # Incomplete JSON, continue receiving
                        continue
                except socket.timeout:
                    logger.warning("Socket timeout during chunked receive")
                    break
                except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
                    logger.error(f"Socket connection error during receive: {str(e)}")
                    raise
        except Exception as e:
            logger.error(f"Error during receive: {str(e)}")
            raise
            
        # If we get here, we either timed out or broke out of the loop
        if chunks:
            data = b''.join(chunks)
            logger.info(f"Returning data after receive completion ({len(data)} bytes)")
            try:
                json.loads(data.decode('utf-8'))
                return data
            except json.JSONDecodeError:
                raise Exception("Incomplete JSON response received")
        else:
            raise Exception("No data received")

    def _note_script_info(self, result: Any) -> None:
        """Adopt the framing the connected script advertises (MCP-03).

        Called with the result of every successful get_script_info
        exchange on this connection. v2 is used only when the result's
        ``agent0_framing`` list contains "v2"; anything else - an older
        script, a missing key - keeps (or returns) this connection to
        v1. The handshake exchange itself is always answered in the
        framing it was sent in, so adopting its result here affects
        only later exchanges. A script downgraded under a live
        connection therefore costs at most one timed-out framed
        exchange: the error drops the socket, and the reconnect
        re-negotiates from v1.
        """
        framing = result.get("agent0_framing") if isinstance(result, dict) else None
        self._framing_v2 = isinstance(framing, list) and "v2" in framing

    def _receive_framed_response(self, sock, buffer_size=8192, deadline=None) -> bytes:
        """Receive exactly one framing-v2 response frame (MCP-03).

        Chunks feed an incremental FrameDecoder and the completed
        payload is parsed once by the caller; the accumulated buffer
        is never re-parsed (the v1 path's quadratic cost, measured at
        ~61x one-shot at 1 MiB and ~133x at 2 MiB). Raises on EOF, on
        an invalid/oversized frame, and on any trailing bytes: the
        script answers one request with exactly one frame, so extra
        data means the stream is desynchronised and the caller must
        drop the connection. The caller sets the per-command budget
        before the exchange (MCP-09) and, since SI-345, passes the
        exchange's absolute monotonic ``deadline``: like
        receive_full_response, this path narrows the socket timeout
        to the time remaining before each recv() (never widening it),
        so the budget governs the whole exchange on this path too.
        """
        decoder = FrameDecoder()
        deadline = _resolve_exchange_deadline(sock, deadline)
        while True:
            payload = decoder.next_frame()
            if payload is not None:
                break
            _apply_exchange_deadline(sock, deadline)
            chunk = sock.recv(buffer_size)
            if not chunk:
                if decoder.pending_bytes:
                    raise Exception(
                        "Connection closed mid-frame (%d bytes buffered)"
                        % decoder.pending_bytes)
                raise Exception("Connection closed before receiving any data")
            decoder.feed(chunk)
        if decoder.pending_bytes:
            raise Exception(
                "Remote Script sent %d trailing bytes after the response "
                "frame; connection desynchronised" % decoder.pending_bytes)
        logger.info(f"Received complete framed response ({len(payload)} bytes)")
        return payload

    def send_command(self, command_type: str, params: Dict[str, Any] = None, request_id: int = None) -> Dict[str, Any]:
        """Send a command to Ableton and return the response.

        Serialised on ``self._lock``: the request and its response are one
        indivisible exchange on a shared socket. Holding the lock across the
        whole round-trip (including reconnect) is what keeps a concurrent
        caller from reading someone else's reply.
        Raises OutcomeUnknownError when the request was fully sent
        but no complete response arrived (timeout, connection loss,
        unparseable response): the command may have been applied, so
        its outcome is unknown and it must not be blindly retried.
        Failures before the send completes and definitive script
        errors raise plain Exceptions, as before. The whole exchange
        runs under the per-command socket budget (see
        _command_timeout), which both receive paths honour.
        """
        with self._lock:
            return self._send_command_locked(command_type, params, request_id)

    def send_batch(self, commands: List[Dict[str, Any]],
                   stop_on_error: bool = False,
                   request_id: int = None) -> Dict[str, Any]:
        """Send an ordered list of commands as ONE execute_batch exchange.

        ``commands`` is a list of wire items, ``{"type": ..., "params":
        {...}}`` (``params`` optional per item). The Remote Script runs
        them in order inside a single main-thread scheduling and replies
        once, so the whole list costs one socket round trip. Returns the
        batch result: ``{"results": [...], "summary": {...}}``, where
        each entry of ``results`` is the response envelope a direct
        ``send_command`` of that item would have produced - for items
        that reach dispatch; an item the script rejects before
        dispatch (malformed, a nested batch) carries a batch-style
        error envelope instead: the same message, without the
        ``result`` key a dispatched call's envelope carries (or a
        ``{"status": "skipped", ...}`` marker under ``stop_on_error``).

        A failed item does NOT raise here - inspect the per-item
        envelopes. This raises only when the batch exchange itself
        fails (batch-level error envelope, timeout, connection loss),
        like send_command. Batching is sequencing, not a transaction:
        a failed item does not roll back the items before it. Requires
        a Remote Script whose handshake advertises ``execute_batch``
        (script 1.7.3+); older scripts answer "Unknown command".

        If the batch exchange itself fails after the request was sent
        (timeout, connection loss, unparseable response), this raises
        OutcomeUnknownError at batch granularity: the batch may have
        partially applied and the per-item outcomes are unknowable, so
        the batch must not be blindly retried.
        """
        if not isinstance(commands, list):
            raise ValueError(
                "commands must be a list of {'type', 'params'} items")
        params = {"commands": commands,
                  "stop_on_error": bool(stop_on_error)}
        return self.send_command("execute_batch", params,
                                 request_id=request_id)

    def _command_timeout(self, command_type: str,
                         params: Dict[str, Any] = None) -> float:
        """Socket budget in seconds for one exchange (MCP-09).

        Long-running commands get their table entry; execute_batch sums
        the per-item budgets (its script-side work is the sum of its
        items), floored at the modifying-command default; other
        modifying commands get 15 s and reads 10 s. The caller applies
        the value to the socket before the request is sent, and since
        MCP-09 neither receive path overrides it, so the budget a
        command is assigned is the budget its exchange actually gets.
        """
        if command_type in _LONG_RUNNING_COMMAND_TIMEOUTS:
            return _LONG_RUNNING_COMMAND_TIMEOUTS[command_type]
        if command_type == "execute_batch":
            timeout = 0.0
            batch_params = params if isinstance(params, dict) else {}
            for batch_item in batch_params.get("commands") or []:
                item_type = (batch_item.get("type")
                             if isinstance(batch_item, dict) else None)
                if item_type in _LONG_RUNNING_COMMAND_TIMEOUTS:
                    timeout += _LONG_RUNNING_COMMAND_TIMEOUTS[item_type]
                elif item_type in _MODIFYING_COMMANDS:
                    timeout += 15.0
                else:
                    timeout += 10.0
            return max(timeout, 15.0)
        return 15.0 if command_type in _MODIFYING_COMMANDS else 10.0


    def _send_command_locked(self, command_type: str, params: Dict[str, Any] = None, request_id: int = None) -> Dict[str, Any]:
        if not self.sock and not self.connect():
            raise ConnectionError("Not connected to Ableton")

        command = {
            "type": command_type,
            "params": params or {}
        }

        # MCP-03: on a connection that negotiated framing v2, every
        # request carries a native request_id - allocated here when the
        # caller did not supply one - so each framed response correlates
        # exactly with its request, and the echo verification (and the
        # agent0_timing capture) below runs for every v2 exchange.
        # Correlation is a per-exchange echo check under the connection
        # lock, not a session-unique id registry: native allocation
        # counts only its own ids, so it can reissue a value a caller
        # supplied explicitly earlier in the session.
        if request_id is None and self._framing_v2:
            request_id = self._next_request_id
            self._next_request_id += 1
            if self._next_request_id > 9007199254740991:
                self._next_request_id = 1

        # MCP-01 (opt-in): attach a request id only when the caller asked
        # for one. The Remote Script echoes it and adds an agent0_timing
        # block; without request_id the wire request is unchanged.
        if request_id is not None:
            if type(request_id) is not int or not 0 <= request_id <= 9007199254740991:
                raise ValueError(
                    "request_id must be an integer in [0, 9007199254740991]")
            command["request_id"] = request_id
        
        # SI-74: serialise and (under framing v2) frame the request
        # BEFORE the exchange's try, beside the request_id validation
        # above. encode_frame refuses a payload outside
        # 2..MAX_FRAME_BYTES - a purely local property of this
        # request: nothing has been sent, the peer is not involved,
        # and the negotiated connection is healthy. Raised inside the
        # try (the pre-SI-74 shape) that FrameError fell into the
        # generic handler, which dropped the connection and reported
        # the local refusal as a "Communication error with Ableton".
        # Refuse here instead, as a ValueError naming the local
        # refusal, so the connection survives for the next command.
        # (A request json.dumps cannot serialise fails at the same
        # line, likewise before any send and with the connection
        # untouched.)
        request_bytes = json.dumps(command).encode('utf-8')
        if self._framing_v2:
            try:
                request_bytes = encode_frame(request_bytes)
            except FrameError as e:
                raise ValueError("Request not sent: %s" % (e,)) from e

        timeout = self._command_timeout(command_type, params)
        if command_type == "execute_batch":
            _unknown_tail = (
                "the batch was fully sent, so it may have partially "
                "applied; per-item outcomes are unknowable - outcome "
                "unknown")
        else:
            _unknown_tail = (
                "the request was fully sent, so the command may have "
                "been applied - outcome unknown")
        
        try:
            if LEAN_PROFILE:
                # Logging diet (MCP-04): the command name and the params
                # size, never the parameter payload itself.
                logger.info(
                    f"Sending command: {command_type} "
                    f"(params: {len(json.dumps(params or {}).encode('utf-8'))} bytes)"
                )
            else:
                logger.info(f"Sending command: {command_type} with params: {params}")
            
            # The per-command budget governs the WHOLE exchange
            # (MCP-09): it is set before the request is sent, so neither
            # the send nor either receive path can run under a leftover
            # timeout from an earlier command (or the blocking mode
            # connect() and the liveness peek leave behind). Before
            # MCP-09 the budget was only set after sendall - and the v1
            # receive path then overrode it with a hard 15 s anyway.
            self.sock.settimeout(timeout)

            # SI-345: the budget is also converted, here, into an
            # absolute monotonic deadline for the exchange (the
            # dataset recorder's flush pattern: deadline computed
            # once, honoured per iteration). settimeout alone applies
            # per recv() call, so a peer delivering at least one byte
            # inside every window could hold either receive path past
            # the budget indefinitely and the timeout classification
            # below never fired (SI-344 D1). The receive paths narrow
            # the socket timeout to the time remaining before each
            # recv(), so a response not complete by this deadline
            # raises the same socket.timeout this budget always
            # produced for a silent peer.
            deadline = time.monotonic() + timeout

            # Send the command (serialised and, under framing v2,
            # framed before the try - SI-74). Under framing v2 the
            # same JSON bytes travel length-prefixed; the script
            # answers in whichever framing the request used, so this
            # choice is per-connection state, never a guess about the
            # peer.
            self.sock.sendall(request_bytes)
            logger.info(f"Command sent, waiting for response...")
            
            # Receive the response. From here on the request has been
            # fully sent, so any failure leaves the command's outcome
            # unknown (MCP-09): it may have been applied in Live. Those
            # failures raise OutcomeUnknownError, distinct from the
            # plain Exceptions raised for pre-send failures and for
            # definitive answers (error envelope, echo mismatch).
            try:
                if self._framing_v2:
                    response_data = self._receive_framed_response(
                        self.sock, deadline=deadline)
                else:
                    response_data = self.receive_full_response(
                        self.sock, deadline=deadline)
            except socket.timeout:
                raise OutcomeUnknownError(
                    "Timeout waiting for Ableton response to %r "
                    "(budget %.0fs); %s"
                    % (command_type, timeout, _unknown_tail),
                    command_type=command_type)
            except (ConnectionError, BrokenPipeError,
                    ConnectionResetError) as e:
                raise OutcomeUnknownError(
                    "Connection to Ableton lost while waiting for the "
                    "response to %r: %s; %s"
                    % (command_type, str(e), _unknown_tail),
                    command_type=command_type) from e
            except Exception as e:
                raise OutcomeUnknownError(
                    "No complete response from Ableton to %r: %s; %s"
                    % (command_type, str(e), _unknown_tail),
                    command_type=command_type) from e
            logger.info(f"Received {len(response_data)} bytes of data")

            # Parse the response
            response = json.loads(response_data.decode('utf-8'))

            # Envelope validation (SI-73, MCP-09): the request has
            # been fully sent, so a reply that is not a script
            # envelope is the same epistemic state as an unparseable
            # reply - nothing definitive is known and the command
            # may have been applied - and classifies as
            # OutcomeUnknownError, not a plain failure a caller
            # could read as safe to retry. The real script always
            # sends {"status": "success", "result": ...} or
            # {"status": "error", "message": ...} (its success
            # envelope always carries a result key), so validation
            # is all-or-nothing: a non-object reply, a missing or
            # unrecognised status, and a success envelope with no
            # result are all non-envelopes. Definitive envelopes
            # below (error, request-id mismatch) stay plain.
            if not isinstance(response, dict):
                raise OutcomeUnknownError(
                    "Invalid response from Ableton to %r: response "
                    "is not a script envelope (expected a JSON "
                    "object, got %s); %s"
                    % (command_type, type(response).__name__,
                       _unknown_tail),
                    command_type=command_type)
            if response.get("status") not in ("success", "error"):
                raise OutcomeUnknownError(
                    "Invalid response from Ableton to %r: envelope "
                    "status is missing or unrecognised (%r); %s"
                    % (command_type, response.get("status"),
                       _unknown_tail),
                    command_type=command_type)
            if (response.get("status") == "success"
                    and "result" not in response):
                raise OutcomeUnknownError(
                    "Invalid response from Ableton to %r: success "
                    "envelope carries no 'result'; %s"
                    % (command_type, _unknown_tail),
                    command_type=command_type)
            logger.info(f"Response parsed, status: {response.get('status', 'unknown')}")

            if response.get("status") == "error":
                logger.error(f"Ableton error: {response.get('message')}")
                raise Exception(response.get("message", "Unknown error from Ableton"))
            
            if request_id is not None:
                if response.get("request_id") != request_id:
                    raise Exception(
                        "Remote Script did not echo request_id %r "
                        "(response carried %r); the loaded script may "
                        "not support request IDs" % (
                            request_id, response.get("request_id")))
                self.last_timing = response.get("agent0_timing")
                self.last_request_id = request_id

            if command_type == "get_script_info":
                # MCP-03 negotiation: this handshake's advertisement
                # decides the framing of the NEXT exchange.
                self._note_script_info(response.get("result"))

            return response["result"]
        except OutcomeUnknownError:
            # Raised by the receive phase above (and the JSONDecodeError
            # handler below): the connection is dropped, the type and
            # message are already the classification - pass it through.
            self._drop_connection()
            raise
        except socket.timeout:
            # Only reachable before the request is fully sent (e.g. a
            # send that blocks until the budget expires): the receive
            # phase converts its own timeouts to OutcomeUnknownError.
            logger.error("Socket timeout while waiting for response from Ableton")
            self._drop_connection()
            raise Exception("Timeout waiting for Ableton response")
        except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
            logger.error(f"Socket connection error: {str(e)}")
            self._drop_connection()
            raise Exception(f"Connection to Ableton lost: {str(e)}")
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            # A complete response arrived but cannot be read - not
            # JSON, or not even valid UTF-8: the command may have
            # been applied and its result is unreadable, so this too
            # is outcome-unknown, not a plain failure.
            logger.error(f"Invalid JSON response from Ableton: {str(e)}")
            if 'response_data' in locals() and response_data:
                logger.error(f"Raw response (first 200 bytes): {response_data[:200]}")
            self._drop_connection()
            raise OutcomeUnknownError(
                "Invalid response from Ableton to %r: %s; %s"
                % (command_type, str(e), _unknown_tail),
                command_type=command_type) from e
        except Exception as e:
            logger.error(f"Error communicating with Ableton: {str(e)}")
            self._drop_connection()
            raise Exception(f"Communication error with Ableton: {str(e)}")

@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    """Manage server startup and shutdown lifecycle"""
    try:
        logger.info("AbletonMCP server starting up")

        # Record startup event for telemetry
        try:
            record_startup()
        except Exception as e:
            logger.debug(f"Failed to record startup telemetry: {e}")

        script_info = None
        try:
            ableton = get_ableton_connection()
            logger.info("Successfully connected to Ableton on startup")
            from .script_handshake import handshake

            script_info = handshake(ableton.send_command)
        except Exception as e:
            logger.warning(f"Could not connect to Ableton on startup: {str(e)}")
            logger.warning("Make sure the Ableton Remote Script is running")

        if LEAN_PROFILE:
            # No dataset/poller startup steps at all (MCP-04); the
            # script handshake above still happened.
            logger.info(
                "Lean profile on (ABLETON_MCP_LEAN_PROFILE) — dataset and "
                "telemetry side plane disabled"
            )
        elif dataset_enabled():
            try:
                recorder = get_recorder()
                if recorder:
                    logger.info(
                        "Dataset recording enabled → Supabase session %s",
                        recorder.session_id,
                    )
                from .script_handshake import script_has_capability
                from .dataset.passive_poller import start_passive_poller

                # Runs even without Live-UI events: it also settles implicit
                # preferences for agent actions.
                start_passive_poller()
                if not script_has_capability("drain_passive_events"):
                    logger.warning(
                        "Passive Live listeners unavailable — Remote Script outdated "
                        "or not loaded. Restart Ableton after script update."
                    )
            except Exception as e:
                logger.warning(f"Failed to start dataset recorder: {e}")
        else:
            reason = "unknown"
            try:
                from .config import telemetry_config  # noqa: F401
                from .telemetry import get_telemetry_consent, is_telemetry_enabled

                if not is_telemetry_enabled():
                    reason = "telemetry disabled (config.enabled=False or DISABLE_TELEMETRY)"
                elif not get_telemetry_consent():
                    reason = "no dataset consent yet — the user has not opted in"
                else:
                    reason = "ABLETON_MCP_DISABLE_DATASET or unexpected gate failure"
            except Exception:
                reason = "MCP_Server/config.py missing or unreadable (gitignored; required for Supabase)"
            logger.info("Dataset recording off — %s", reason)

        yield {"script_info": script_info}
    finally:
        global _ableton_connection
        if not LEAN_PROFILE:
            try:
                from .dataset.passive_poller import stop_passive_poller

                stop_passive_poller()
            except Exception as e:
                logger.debug(f"Failed to stop passive poller: {e}")
        if _ableton_connection:
            logger.info("Disconnecting from Ableton on shutdown")
            _ableton_connection.disconnect()
            _ableton_connection = None
        try:
            recorder = get_recorder()
            if recorder:
                # Bounded wait so queued rows land before the daemon worker
                # is killed at exit.
                recorder.end(timeout=float(
                    os.environ.get("ABLETON_MCP_DATASET_FLUSH_SEC", "5")
                ))
        except Exception as e:
            logger.debug(f"Failed to end dataset session: {e}")
        logger.info("AbletonMCP server shut down")

# Create the MCP server with lifespan support
mcp = FastMCP(
    "AbletonMCP",
    lifespan=server_lifespan
)

# Global connection for resources
_ableton_connection = None

def get_ableton_connection():
    """Get or create a persistent Ableton connection"""
    global _ableton_connection

    if _ableton_connection is not None and _ableton_connection.sock is not None:
        try:
            # Check if the socket is still alive by peeking for data
            # MSG_PEEK + MSG_DONTWAIT will raise BlockingIOError if alive but no data,
            # or return b'' if the remote end has closed the connection.
            _ableton_connection.sock.setblocking(False)
            try:
                data = _ableton_connection.sock.recv(1, socket.MSG_PEEK)
                if data == b'':
                    raise ConnectionError("Remote end closed")
            except BlockingIOError:
                pass  # Socket is alive, just no data waiting — this is normal
            finally:
                _ableton_connection.sock.setblocking(True)
            return _ableton_connection
        except Exception as e:
            logger.warning(f"Existing connection is no longer valid: {str(e)}")
            try:
                _ableton_connection.disconnect()
            except:
                pass
            _ableton_connection = None
            # Passive events stop arriving while disconnected, so any cached
            # state hash may describe a Live session that has since moved on.
            try:
                recorder = get_recorder()
                if recorder:
                    recorder.invalidate_cached_state()
            except Exception:
                pass
    
    # Connection doesn't exist or is invalid, create a new one
    if _ableton_connection is None:
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(f"Connecting to Ableton at {ABLETON_HOST}:{ABLETON_PORT} (attempt {attempt}/{max_attempts})...")
                _ableton_connection = AbletonConnection(host=ABLETON_HOST, port=ABLETON_PORT)
                if _ableton_connection.connect():
                    logger.info("Created new persistent connection to Ableton")
                    return _ableton_connection
                else:
                    _ableton_connection = None
            except Exception as e:
                logger.error(f"Connection attempt {attempt} failed: {str(e)}")
                if _ableton_connection:
                    _ableton_connection.disconnect()
                    _ableton_connection = None

            if attempt < max_attempts:
                import time
                time.sleep(1.0)
        
        # If we get here, all connection attempts failed
        if _ableton_connection is None:
            logger.error("Failed to connect to Ableton after multiple attempts")
            raise Exception("Could not connect to Ableton. Make sure the Remote Script is running.")
    
    return _ableton_connection


def _dump_tool_result(value: Any) -> str:
    """Serialise a Live-facing tool result for return to the client.

    The default profile pretty-prints with indent=2, byte-identical to
    the historical output. The lean profile (MCP-04) returns the same
    JSON value compactly — no pretty-printing — removing the size
    inflation the audit measured on note/snapshot results. Only JSON
    dumps go through here; string-rendered tools (browser tree, drum
    kit, short-sentence results) keep their own rendering.
    """
    if LEAN_PROFILE:
        return json.dumps(value, separators=(",", ":"))
    return json.dumps(value, indent=2)


# Core Tool endpoints

@mcp.tool()
def set_dataset_consent(ctx: Context, consent: bool, user_said: str = "") -> str:
    """Record the user's answer to the dataset consent question.

    Call this ONLY after the user has answered in their own words. Never infer
    the answer, never call it on their behalf, and never call it because
    contributing seems helpful — consent the user did not give is not consent.
    If they have not been asked yet, ask first and wait for their reply.

    Parameters:
    - consent: True if the user agreed to contribute, False if they declined
    - user_said: The user's reply, quoted as closely as possible
    """
    # Intentionally undecorated: recording this call would mean collecting data
    # from someone who may have just declined.
    try:
        from .dataset.consent import record_consent

        state = record_consent(consent, quote=user_said)
    except Exception as e:
        logger.error(f"Could not record dataset consent: {str(e)}")
        return f"Error recording consent: {str(e)}"

    if not consent:
        return (
            "Recorded: dataset contribution declined. Nothing is uploaded, and "
            "you will not be asked again."
        )

    if LEAN_PROFILE:
        # Lean profile (MCP-04): the consent answer above is still the
        # user's to record, but recording itself is structurally off.
        # Mirror the disabled-switch outcome — the "recording is
        # unavailable" return below — without refreshing telemetry
        # consent, touching the recorder, or starting the poller (the
        # poller's own gate knows nothing of the lean profile, so a
        # stored grant would otherwise start it).
        recorder = None
    else:
        try:
            from .telemetry import refresh_consent_from_dataset

            refresh_consent_from_dataset()
            recorder = get_recorder()
            # Under opt-in the poller is almost never started at boot — consent did
            # not exist yet — so a grant has to start it here or passive Live events
            # would be missed for the rest of the session. Idempotent and
            # self-gating, so this is safe even if startup did start it.
            from .dataset.passive_poller import start_passive_poller

            start_passive_poller()
        except Exception as e:
            logger.error(f"Consent saved but recording failed to start: {str(e)}")
            return (
                "Consent saved, but recording could not start in this session. "
                "It will begin after a restart."
            )

    if recorder is None:
        return (
            "Consent saved, but recording is unavailable (telemetry may be "
            f"disabled). State: {state}."
        )
    return (
        "Thank you — this session is now being contributed to the open dataset. "
        "Say so at any time to stop, or set ABLETON_MCP_DISABLE_DATASET=1."
    )


@mcp.tool()
@telemetry_tool("get_session_info")
@trajectory_tool("get_session_info")
def get_session_info(ctx: Context, user_prompt: str = "") -> str:
    """Get detailed information about the current Ableton session

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_session_info")
        return _dump_tool_result(result)
    except Exception as e:
        logger.error(f"Error getting session info from Ableton: {str(e)}")
        return f"Error getting session info: {str(e)}"


@mcp.tool()
@telemetry_tool("get_remote_script_info")
@trajectory_tool("get_remote_script_info", modifying=False)
def get_remote_script_info(ctx: Context, user_prompt: str = "") -> str:
    """
    Report Ableton Remote Script version and capabilities (handshake).

    Use this to verify the Live-side bridge matches this MCP server package.
    """
    try:
        from .remote_script_install import EXPECTED_REMOTE_SCRIPT_VERSION
        from .script_handshake import get_cached_script_info, handshake

        ableton = get_ableton_connection()
        info = handshake(ableton.send_command)
        cached = dict(get_cached_script_info() or info)
        cached["expected_version"] = EXPECTED_REMOTE_SCRIPT_VERSION
        return _dump_tool_result(cached)
    except Exception as e:
        logger.error(f"Error getting remote script info: {str(e)}")
        return f"Error getting remote script info: {str(e)}"

@mcp.tool()
@telemetry_tool("get_track_info")
@trajectory_tool("get_track_info")
def get_track_info(
    ctx: Context,
    track_index: int,
    include_clip_slots: bool = True,
    include_devices: bool = True,
    user_prompt: str = "",
) -> str:
    """
    Get detailed information about a specific track in Ableton.

    Parameters:
    - track_index: The index of the track to get information about
    - include_clip_slots: Include the clip-slot list (default True;
      False omits it from the result entirely)
    - include_devices: Include the device list (default True;
      False omits it from the result entirely)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        params: Dict[str, Any] = {"track_index": track_index}
        # MCP-05 projection: forward only non-default flags, so a
        # default call sends exactly the pre-projection params.
        if not include_clip_slots:
            params["include_clip_slots"] = False
        if not include_devices:
            params["include_devices"] = False
        result = ableton.send_command("get_track_info", params)
        return _dump_tool_result(result)
    except Exception as e:
        logger.error(f"Error getting track info from Ableton: {str(e)}")
        return f"Error getting track info: {str(e)}"


@mcp.tool()
@telemetry_tool("get_clip_notes")
@trajectory_tool("get_clip_notes")
def get_clip_notes(
    ctx: Context,
    track_index: int,
    clip_index: int,
    user_prompt: str = "",
) -> str:
    """
    Read all MIDI notes from a Session-view clip.

    Returns pitch, start_time, duration, velocity, mute (and extended fields when available).

    Parameters:
    - track_index: Track that owns the clip
    - clip_index: Session clip slot index
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        missing = require_capability("get_clip_notes")
        if missing:
            return missing
        ableton = get_ableton_connection()
        result = ableton.send_command(
            "get_clip_notes",
            {"track_index": track_index, "clip_index": clip_index},
        )
        return _dump_tool_result(result)
    except Exception as e:
        logger.error(f"Error getting clip notes: {str(e)}")
        return f"Error getting clip notes: {str(e)}"


@mcp.tool()
@telemetry_tool("get_device_parameters")
@trajectory_tool("get_device_parameters")
def get_device_parameters(
    ctx: Context,
    track_index: int,
    device_index: int,
    parameter_indices: Optional[List[int]] = None,
    parameter_names: Optional[List[str]] = None,
    user_prompt: str = "",
) -> str:
    """
    Read all parameters for a device on a track (name, value, min, max).

    Parameters:
    - track_index: Track that owns the device
    - device_index: Index into the track's device chain
    - parameter_indices: Optional subset - return only the parameters
      with these indices (default: all parameters)
    - parameter_names: Optional subset - return only the parameters
      with these names; combined with parameter_indices as a union
      (default: all parameters)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        params: Dict[str, Any] = {
            "track_index": track_index, "device_index": device_index}
        # MCP-06 subset: forward only the selectors the caller actually
        # set, so a default call sends exactly the pre-subset params.
        if parameter_indices is not None:
            params["parameter_indices"] = parameter_indices
        if parameter_names is not None:
            params["parameter_names"] = parameter_names
        result = ableton.send_command("get_device_parameters", params)
        return _dump_tool_result(result)
    except Exception as e:
        logger.error(f"Error getting device parameters: {str(e)}")
        return f"Error getting device parameters: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("set_device_parameter")
@trajectory_tool("set_device_parameter")
def set_device_parameter(
    ctx: Context,
    track_index: int,
    device_index: int,
    parameter_index: int,
    value: float,
    user_prompt: str = "",
) -> str:
    """
    Set a device parameter to a specific value.

    Use get_device_parameters first to discover parameter indices and ranges.

    Parameters:
    - track_index: Track that owns the device
    - device_index: Index into the track's device chain
    - parameter_index: Index into device.parameters
    - value: New parameter value (Live parameter units)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command(
            "set_device_parameter",
            {
                "track_index": track_index,
                "device_index": device_index,
                "parameter_index": parameter_index,
                "value": value,
            },
        )
        return (
            f"Set {result.get('name', 'parameter')} "
            f"{result.get('old_value')} → {result.get('value')}"
        )
    except Exception as e:
        logger.error(f"Error setting device parameter: {str(e)}")
        return f"Error setting device parameter: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("set_device_parameters")
@trajectory_tool("set_device_parameters")
def set_device_parameters(
    ctx: Context,
    track_index: int,
    device_index: int,
    parameters: List[Dict[str, Any]],
    stop_on_error: bool = False,
    user_prompt: str = "",
) -> str:
    """
    Set several parameters on ONE device in a single call.

    Every item is applied with exactly set_device_parameter's
    semantics and reported individually in the result (old and new
    values per item); one bad item does not block the others unless
    stop_on_error is set. An item that never reaches the set
    (malformed, unaddressable, unknown name) reports a batch-style
    per-item error instead: the same message a direct call would
    produce, without the dispatched call's "result": {} key; items
    that reach the set return the direct call's envelope exactly.
    This is not a transaction: items apply in
    order and a failed item does not roll back the ones before it.

    Parameters:
    - track_index: Track that owns the device
    - device_index: Index into the track's device chain
    - parameters: List of {"parameter_index": int, "value": float}
      items; an item may carry "name" instead of "parameter_index" to
      address a parameter by name (parameter_index wins if both given)
    - stop_on_error: Report items after the first failure as skipped
      instead of applying them (default False)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        params: Dict[str, Any] = {
            "track_index": track_index,
            "device_index": device_index,
            "parameters": parameters,
        }
        if stop_on_error:
            params["stop_on_error"] = True
        result = ableton.send_command("set_device_parameters", params)
        return _dump_tool_result(result)
    except Exception as e:
        logger.error(f"Error setting device parameters: {str(e)}")
        return f"Error setting device parameters: {str(e)}"


@mcp.tool()
@telemetry_tool("get_session_snapshot")
@trajectory_tool("get_session_snapshot")
def get_session_snapshot(
    ctx: Context,
    include_notes: bool = True,
    include_params: bool = True,
    track_indices: Optional[List[int]] = None,
    include_scenes: bool = True,
    include_return_tracks: bool = True,
    include_master_track: bool = True,
    include_cue_points: bool = True,
    include_clip_slots: bool = True,
    include_devices: bool = True,
    include_arrangement_clips: bool = True,
    include_automation: Optional[bool] = None,
    user_prompt: str = "",
) -> str:
    """
    Capture a full project state snapshot for trajectory recording.

    Includes session metadata, every track (mixer, devices, session clips,
    arrangement clips), optional MIDI notes, and optional device parameters.
    Used to version musical state S_t → S_{t+1}.

    Parameters:
    - include_notes: Include MIDI note arrays in clips (default True)
    - include_params: Include device parameter values (default True)
    - track_indices: Restrict the snapshot to these track indices
      (default: all tracks)
    - include_scenes / include_return_tracks / include_master_track /
      include_cue_points: Include that top-level section (default True;
      False omits it from the result entirely)
    - include_clip_slots / include_devices / include_arrangement_clips:
      Include that per-track section (default True; False omits it)
    - include_automation: Run the automated-parameter scan that feeds
      the automated_parameters keys on devices (default: follows
      include_params; set explicitly to override either way)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        missing = require_capability("get_session_snapshot")
        if missing:
            return missing
        ableton = get_ableton_connection()
        params: Dict[str, Any] = {
            "include_notes": include_notes,
            "include_params": include_params,
        }
        # MCP-05 projection: forward only what the caller actually set,
        # so a default call sends exactly the pre-projection params.
        if track_indices is not None:
            params["track_indices"] = track_indices
        if include_automation is not None:
            params["include_automation"] = include_automation
        for _key, _value in (
            ("include_scenes", include_scenes),
            ("include_return_tracks", include_return_tracks),
            ("include_master_track", include_master_track),
            ("include_cue_points", include_cue_points),
            ("include_clip_slots", include_clip_slots),
            ("include_devices", include_devices),
            ("include_arrangement_clips", include_arrangement_clips),
        ):
            if not _value:
                params[_key] = False
        result = ableton.send_command("get_session_snapshot", params)
        return _dump_tool_result(result)
    except Exception as e:
        logger.error(f"Error getting session snapshot: {str(e)}")
        return f"Error getting session snapshot: {str(e)}"


@mcp.tool()
@telemetry_tool("create_midi_track")
@trajectory_tool("create_midi_track")
def create_midi_track(ctx: Context, index: int = -1, user_prompt: str = "") -> str:
    """
    Create a new MIDI track in the Ableton session.

    Parameters:
    - index: The index to insert the track at (-1 = end of list)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_midi_track", {"index": index})
        return f"Created new MIDI track: {result.get('name', 'unknown')}"
    except Exception as e:
        logger.error(f"Error creating MIDI track: {str(e)}")
        return f"Error creating MIDI track: {str(e)}"


@mcp.tool()
@telemetry_tool("create_audio_track")
@trajectory_tool("create_audio_track")
def create_audio_track(ctx: Context, index: int = -1, user_prompt: str = "") -> str:
    """
    Create a new audio track in the Ableton session.

    Use this for recorded or imported audio (samples, stems, vocals). For MIDI
    instruments use create_midi_track instead.

    Parameters:
    - index: The index to insert the track at (-1 = end of list)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_audio_track", {"index": index})
        return f"Created new audio track: {result.get('name', 'unknown')}"
    except Exception as e:
        logger.error(f"Error creating audio track: {str(e)}")
        return f"Error creating audio track: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("set_track_name")
@trajectory_tool("set_track_name")
def set_track_name(ctx: Context, track_index: int, name: str, user_prompt: str = "") -> str:
    """
    Set the name of a track.

    Parameters:
    - track_index: The index of the track to rename
    - name: The new name for the track
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_track_name", {"track_index": track_index, "name": name})
        return f"Renamed track to: {result.get('name', name)}"
    except Exception as e:
        logger.error(f"Error setting track name: {str(e)}")
        return f"Error setting track name: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("create_clip")
@trajectory_tool("create_clip")
def create_clip(ctx: Context, track_index: int, clip_index: int, length: float = 4.0, user_prompt: str = "") -> str:
    """
    Create a new MIDI clip in the specified track and clip slot.

    Parameters:
    - track_index: The index of the track to create the clip in
    - clip_index: The index of the clip slot to create the clip in
    - length: The length of the clip in beats (default: 4.0)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_clip", {
            "track_index": track_index, 
            "clip_index": clip_index, 
            "length": length
        })
        return f"Created new clip at track {track_index}, slot {clip_index} with length {length} beats"
    except Exception as e:
        logger.error(f"Error creating clip: {str(e)}")
        return f"Error creating clip: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("create_audio_clip")
@trajectory_tool("create_audio_clip")
def create_audio_clip(ctx: Context, track_index: int, clip_index: int, path: str, user_prompt: str = "") -> str:
    """
    Create a new audio clip in an audio track's clip slot by importing a file.

    Requires Ableton Live 12.0.5 or newer — the underlying
    ClipSlot.create_audio_clip Live API was introduced in 12.0.5 and is not
    available in earlier 12.0.x releases.

    Parameters:
    - track_index: The index of the audio track to create the clip in
    - clip_index: The index of the clip slot to create the clip in
    - path: Absolute path to a supported audio file (e.g. a .wav). The target
      track must be an audio track and the clip slot must be empty.
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_audio_clip", {
            "track_index": track_index,
            "clip_index": clip_index,
            "path": path
        })
        return f"Created audio clip '{result.get('name', 'clip')}' at track {track_index}, slot {clip_index} (length {result.get('length', '?')} beats)"
    except Exception as e:
        logger.error(f"Error creating audio clip: {str(e)}")
        return f"Error creating audio clip: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("add_notes_to_clip", capture_notes=True)
@trajectory_tool("add_notes_to_clip")
def add_notes_to_clip(
    ctx: Context,
    track_index: int,
    clip_index: int,
    notes: List[Dict[str, Union[int, float, bool]]],
    user_prompt: str = ""
) -> str:
    """
    Add MIDI notes to a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - notes: List of note dictionaries, each with pitch, start_time, duration, velocity, and mute
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("add_notes_to_clip", {
            "track_index": track_index,
            "clip_index": clip_index,
            "notes": notes
        })
        return f"Added {len(notes)} notes to clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        logger.error(f"Error adding notes to clip: {str(e)}")
        return f"Error adding notes to clip: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("clear_notes_from_clip")
def clear_notes_from_clip(
    ctx: Context,
    track_index: int,
    clip_index: int,
    user_prompt: str = ""
) -> str:
    """
    Remove all MIDI notes from a Session clip.

    Writes are additive (add_notes_to_clip only appends), so to truly *modify*
    a clip you clear it first, then add the new notes. Use this with
    get_clip_notes and add_notes_to_clip for a real read -> modify -> write
    loop: read the notes, edit the list, clear_notes_from_clip, then
    add_notes_to_clip the edited notes.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        missing = require_capability("clear_notes_from_clip")
        if missing:
            return missing
        ableton = get_ableton_connection()
        result = ableton.send_command("clear_notes_from_clip", {
            "track_index": track_index,
            "clip_index": clip_index
        })
        return "Cleared {n} note(s) from clip '{name}' (track {t}, slot {c})".format(
            n=result.get("cleared_count", "?"),
            name=result.get("clip_name", "clip"),
            t=track_index,
            c=clip_index,
        )
    except Exception as e:
        logger.error(f"Error clearing notes from clip: {str(e)}")
        return f"Error clearing notes from clip: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("set_clip_name")
@trajectory_tool("set_clip_name")
def set_clip_name(ctx: Context, track_index: int, clip_index: int, name: str, user_prompt: str = "") -> str:
    """
    Set the name of a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - name: The new name for the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_clip_name", {
            "track_index": track_index,
            "clip_index": clip_index,
            "name": name
        })
        return f"Renamed clip at track {track_index}, slot {clip_index} to '{name}'"
    except Exception as e:
        logger.error(f"Error setting clip name: {str(e)}")
        return f"Error setting clip name: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("set_arrangement_clip_name")
@trajectory_tool("set_arrangement_clip_name")
def set_arrangement_clip_name(ctx: Context, track_index: int, clip_index: int, name: str, user_prompt: str = "") -> str:
    """
    Set the name of a clip placed in the Arrangement timeline.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip within track.arrangement_clips, in the
      same order returned by get_arrangement_clips (i.e. ordered by start_time)
    - name: The new name for the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_arrangement_clip_name", {
            "track_index": track_index,
            "clip_index": clip_index,
            "name": name
        })
        return f"Renamed arrangement clip at track {track_index}, index {clip_index} to '{name}'"
    except Exception as e:
        logger.error(f"Error setting arrangement clip name: {str(e)}")
        return f"Error setting arrangement clip name: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("set_tempo")
@trajectory_tool("set_tempo")
def set_tempo(ctx: Context, tempo: float, user_prompt: str = "") -> str:
    """
    Set the tempo of the Ableton session.

    Parameters:
    - tempo: The new tempo in BPM
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_tempo", {"tempo": tempo})
        return f"Set tempo to {tempo} BPM"
    except Exception as e:
        logger.error(f"Error setting tempo: {str(e)}")
        return f"Error setting tempo: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("load_instrument_or_effect")
@trajectory_tool("load_instrument_or_effect")
def load_instrument_or_effect(ctx: Context, track_index: int, uri: str, user_prompt: str = "") -> str:
    """
    Load an instrument or effect onto a track using its URI.

    Parameters:
    - track_index: The index of the track to load the instrument on
    - uri: The URI of the instrument or effect to load (e.g., 'query:Synths#Instrument%20Rack:Bass:FileId_5116')
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("load_browser_item", {
            "track_index": track_index,
            "item_uri": uri
        })
        
        # Check if the instrument was loaded successfully
        if result.get("loaded", False):
            new_devices = result.get("new_devices", [])
            if new_devices:
                return f"Loaded instrument with URI '{uri}' on track {track_index}. New devices: {', '.join(new_devices)}"
            else:
                devices = result.get("devices_after", [])
                return f"Loaded instrument with URI '{uri}' on track {track_index}. Devices on track: {', '.join(devices)}"
        else:
            return f"Failed to load instrument with URI '{uri}'"
    except Exception as e:
        logger.error(f"Error loading instrument by URI: {str(e)}")
        return f"Error loading instrument by URI: {str(e)}"

@mcp.tool()
@telemetry_tool("fire_clip")
@trajectory_tool("fire_clip")
def fire_clip(ctx: Context, track_index: int, clip_index: int, user_prompt: str = "") -> str:
    """
    Start playing a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("fire_clip", {
            "track_index": track_index,
            "clip_index": clip_index
        })
        return f"Started playing clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        logger.error(f"Error firing clip: {str(e)}")
        return f"Error firing clip: {str(e)}"

@mcp.tool()
@telemetry_tool("stop_clip")
@trajectory_tool("stop_clip")
def stop_clip(ctx: Context, track_index: int, clip_index: int, user_prompt: str = "") -> str:
    """
    Stop playing a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("stop_clip", {
            "track_index": track_index,
            "clip_index": clip_index
        })
        return f"Stopped clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        logger.error(f"Error stopping clip: {str(e)}")
        return f"Error stopping clip: {str(e)}"


@mcp.tool()
@telemetry_tool("delete_clip")
def delete_clip(ctx: Context, track_index: int, clip_index: int, user_prompt: str = "") -> str:
    """
    Delete the clip in the given clip slot, freeing it for reuse.

    Use this before create_clip when you want to overwrite an existing clip
    (create_clip itself refuses to write into an occupied slot).

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot to clear
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        missing = require_capability("delete_clip")
        if missing:
            return missing
        ableton = get_ableton_connection()
        result = ableton.send_command("delete_clip", {
            "track_index": track_index,
            "clip_index": clip_index,
        })
        return _dump_tool_result(result)
    except Exception as e:
        logger.error(f"Error deleting clip: {str(e)}")
        return f"Error deleting clip: {str(e)}"


@mcp.tool()
@telemetry_tool("start_playback")
@trajectory_tool("start_playback")
def start_playback(ctx: Context, user_prompt: str = "") -> str:
    """Start playing the Ableton session.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("start_playback")
        return "Started playback"
    except Exception as e:
        logger.error(f"Error starting playback: {str(e)}")
        return f"Error starting playback: {str(e)}"

@mcp.tool()
@telemetry_tool("stop_playback")
@trajectory_tool("stop_playback")
def stop_playback(ctx: Context, user_prompt: str = "") -> str:
    """Stop playing the Ableton session.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("stop_playback")
        return "Stopped playback"
    except Exception as e:
        logger.error(f"Error stopping playback: {str(e)}")
        return f"Error stopping playback: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("get_browser_tree")
@trajectory_tool("get_browser_tree")
def get_browser_tree(ctx: Context, category_type: str = "all", user_prompt: str = "") -> str:
    """
    Get a hierarchical tree of browser categories from Ableton.

    Parameters:
    - category_type: Type of categories to get ('all', 'instruments', 'sounds', 'drums', 'audio_effects', 'midi_effects')
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_browser_tree", {
            "category_type": category_type
        })
        
        # Check if we got any categories
        if "available_categories" in result and len(result.get("categories", [])) == 0:
            available_cats = result.get("available_categories", [])
            return (f"No categories found for '{category_type}'. "
                   f"Available browser categories: {', '.join(available_cats)}")
        
        # Format the tree in a more readable way
        total_folders = result.get("total_folders", 0)
        formatted_output = f"Browser tree for '{category_type}' (showing {total_folders} folders):\n\n"
        
        def format_tree(item, indent=0):
            output = ""
            if item:
                prefix = "  " * indent
                name = item.get("name", "Unknown")
                path = item.get("path", "")
                has_more = item.get("has_more", False)
                
                # Add this item
                output += f"{prefix}• {name}"
                if path:
                    output += f" (path: {path})"
                if has_more:
                    output += " [...]"
                output += "\n"
                
                # Add children
                for child in item.get("children", []):
                    output += format_tree(child, indent + 1)
            return output
        
        # Format each category
        for category in result.get("categories", []):
            formatted_output += format_tree(category)
            formatted_output += "\n"
        
        return formatted_output
    except Exception as e:
        error_msg = str(e)
        if "Browser is not available" in error_msg:
            logger.error(f"Browser is not available in Ableton: {error_msg}")
            return f"Error: The Ableton browser is not available. Make sure Ableton Live is fully loaded and try again."
        elif "Could not access Live application" in error_msg:
            logger.error(f"Could not access Live application: {error_msg}")
            return f"Error: Could not access the Ableton Live application. Make sure Ableton Live is running and the Remote Script is loaded."
        else:
            logger.error(f"Error getting browser tree: {error_msg}")
            return f"Error getting browser tree: {error_msg}"

@mcp.tool()
@rich_telemetry_tool("get_browser_items_at_path")
@trajectory_tool("get_browser_items_at_path")
def get_browser_items_at_path(ctx: Context, path: str, limit: Optional[int] = None, offset: Optional[int] = None, user_prompt: str = "") -> str:
    """
    Get browser items at a specific path in Ableton's browser.

    Parameters:
    - path: Path in the format "category/folder/subfolder"
            where category is one of the available browser categories in Ableton
    - limit: Optional page size (MCP-07). When set (with or without
             offset), only that slice of the items is returned and the
             result reports "total" and "next_offset". Unset, every
             item at the path is returned, as before.
    - offset: Optional page offset (MCP-07).
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        # MCP-07 paging: forward only the values the caller set, so a
        # default call sends exactly the pre-paging params.
        params: Dict[str, Any] = {"path": path}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        result = ableton.send_command("get_browser_items_at_path", params)
        
        # Check if there was an error with available categories
        if "error" in result and "available_categories" in result:
            error = result.get("error", "")
            available_cats = result.get("available_categories", [])
            return (f"Error: {error}\n"
                   f"Available browser categories: {', '.join(available_cats)}")
        
        return _dump_tool_result(result)
    except Exception as e:
        error_msg = str(e)
        if "Browser is not available" in error_msg:
            logger.error(f"Browser is not available in Ableton: {error_msg}")
            return f"Error: The Ableton browser is not available. Make sure Ableton Live is fully loaded and try again."
        elif "Could not access Live application" in error_msg:
            logger.error(f"Could not access Live application: {error_msg}")
            return f"Error: Could not access the Ableton Live application. Make sure Ableton Live is running and the Remote Script is loaded."
        elif "Unknown or unavailable category" in error_msg:
            logger.error(f"Invalid browser category: {error_msg}")
            return f"Error: {error_msg}. Please check the available categories using get_browser_tree."
        elif "Path part" in error_msg and "not found" in error_msg:
            logger.error(f"Path not found: {error_msg}")
            return f"Error: {error_msg}. Please check the path and try again."
        else:
            logger.error(f"Error getting browser items at path: {error_msg}")
            return f"Error getting browser items at path: {error_msg}"

@mcp.tool()
@rich_telemetry_tool("search_browser")
@trajectory_tool("search_browser")
def search_browser(ctx: Context, query: str, limit: int = 50, offset: int = 0, user_prompt: str = "") -> str:
    """
    Search Ableton's browser by item name or URI, one page at a time.

    The search runs in the Remote Script (MCP-07), so finding an item
    no longer means fetching whole subtrees and filtering them here.
    Matching is a case-insensitive substring test against each item's
    name and URI, in browser order.

    Parameters:
    - query: Text to match against item names and URIs (required)
    - limit: Page size (default 50, script-capped at 200)
    - offset: Page offset (default 0)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)

    Returns the page as JSON: {"query", "total", "offset", "limit",
    "next_offset", "items"}, each item carrying its browser "path"
    (e.g. "instruments/Kits/808 Core Kit") for use with
    get_browser_items_at_path or a load call. Requires Remote Script
    1.7.7+; an older script answers with the standard
    "Unknown command: search_browser" error.
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("search_browser", {
            "query": query,
            "limit": limit,
            "offset": offset
        })
        return _dump_tool_result(result)
    except Exception as e:
        logger.error(f"Error searching browser: {str(e)}")
        return f"Error searching browser: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("refresh_browser_cache")
@trajectory_tool("refresh_browser_cache")
def refresh_browser_cache(ctx: Context, user_prompt: str = "") -> str:
    """
    Drop the Remote Script's browser caches (MCP-07).

    The script caches browser reads and URI resolutions so repeated
    browsing doesn't re-walk the hierarchy. Call this after changing
    the library from outside the script - installing a pack, editing
    the user library, rescanning - so the next read sees the change.
    (A load through the script invalidates the caches itself.)

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)

    Returns JSON reporting how many cached entries were dropped.
    Requires Remote Script 1.7.7+.
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("refresh_browser_cache", {})
        return _dump_tool_result(result)
    except Exception as e:
        logger.error(f"Error refreshing browser cache: {str(e)}")
        return f"Error refreshing browser cache: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("set_passive_listeners")
@trajectory_tool("set_passive_listeners")
def set_passive_listeners(ctx: Context, enabled: bool = True, user_prompt: str = "") -> str:
    """
    Enable or disable the Remote Script's passive LOM listeners.

    Since MCP-08 (Remote Script 1.7.8) the script registers its
    passive listeners - the song/track/clip census whose events feed
    drain_passive_events - only when a consumer asks. The dataset
    poller enables them automatically while dataset consent is on;
    call this tool to run the event flow without the dataset, or to
    switch the listeners off again. Toggling is idempotent.

    Parameters:
    - enabled: True registers the listeners, False removes them all
      (default True)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)

    Returns JSON: {"enabled", "changed", "listener_count"}.
    Requires Remote Script 1.7.8+; an older script answers with the
    standard "Unknown command: set_passive_listeners" error (its
    listeners are always registered and cannot be toggled).
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command(
            "set_passive_listeners", {"enabled": bool(enabled)})
        return _dump_tool_result(result)
    except Exception as e:
        logger.error(f"Error setting passive listeners: {str(e)}")
        return f"Error setting passive listeners: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("load_drum_kit")
@trajectory_tool("load_drum_kit")
def load_drum_kit(ctx: Context, track_index: int, rack_uri: str, kit_path: str, user_prompt: str = "") -> str:
    """
    Load a drum rack and then load a specific drum kit into it.

    Parameters:
    - track_index: The index of the track to load on
    - rack_uri: The URI of the drum rack to load (e.g., 'Drums/Drum Rack')
    - kit_path: Path to the drum kit inside the browser (e.g., 'drums/acoustic/kit1')
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        
        # Step 1: Load the drum rack
        result = ableton.send_command("load_browser_item", {
            "track_index": track_index,
            "item_uri": rack_uri
        })
        
        if not result.get("loaded", False):
            return f"Failed to load drum rack with URI '{rack_uri}'"
        
        # Step 2: Get the drum kit items at the specified path
        kit_result = ableton.send_command("get_browser_items_at_path", {
            "path": kit_path
        })
        
        if "error" in kit_result:
            return f"Loaded drum rack but failed to find drum kit: {kit_result.get('error')}"
        
        # Step 3: Find a loadable drum kit
        kit_items = kit_result.get("items", [])
        loadable_kits = [item for item in kit_items if item.get("is_loadable", False)]
        
        if not loadable_kits:
            return f"Loaded drum rack but no loadable drum kits found at '{kit_path}'"
        
        # Step 4: Load the first loadable kit
        kit_uri = loadable_kits[0].get("uri")
        load_result = ableton.send_command("load_browser_item", {
            "track_index": track_index,
            "item_uri": kit_uri
        })
        
        return f"Loaded drum rack and kit '{loadable_kits[0].get('name')}' on track {track_index}"
    except Exception as e:
        logger.error(f"Error loading drum kit: {str(e)}")
        return f"Error loading drum kit: {str(e)}"

# ── Arrangement view tools ────────────────────────────────────────────────────

@mcp.tool()
@telemetry_tool("switch_to_arrangement_view")
@trajectory_tool("switch_to_arrangement_view")
def switch_to_arrangement_view(ctx: Context, user_prompt: str = "") -> str:
    """Switch Ableton's main window to the Arrangement view.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        ableton.send_command("switch_to_arrangement_view")
        return "Switched to Arrangement view"
    except Exception as e:
        logger.error(f"Error switching to arrangement view: {str(e)}")
        return f"Error switching to arrangement view: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("set_arrangement_time")
@trajectory_tool("set_arrangement_time")
def set_arrangement_time(ctx: Context, time: float, user_prompt: str = "") -> str:
    """
    Move the arrangement playhead to a specific position.

    Parameters:
    - time: Position in beats from the start of the arrangement (e.g. 8.0 = bar 3 in 4/4)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_current_song_time", {"time": time})
        return f"Playhead moved to beat {result.get('current_song_time', time)}"
    except Exception as e:
        logger.error(f"Error setting arrangement time: {str(e)}")
        return f"Error setting arrangement time: {str(e)}"


@mcp.tool()
@telemetry_tool("get_arrangement_clips")
@trajectory_tool("get_arrangement_clips")
def get_arrangement_clips(ctx: Context, track_index: int, user_prompt: str = "") -> str:
    """
    List all clips placed in the Arrangement timeline for a track.

    Returns each clip's name, start_time, end_time, length, and type.

    Parameters:
    - track_index: The index of the track to inspect
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_arrangement_clips", {"track_index": track_index})
        return _dump_tool_result(result)
    except Exception as e:
        logger.error(f"Error getting arrangement clips: {str(e)}")
        return f"Error getting arrangement clips: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("duplicate_to_arrangement")
@trajectory_tool("duplicate_to_arrangement")
def duplicate_to_arrangement(
    ctx: Context,
    track_index: int,
    clip_index: int,
    destination_time: float,
    user_prompt: str = ""
) -> str:
    """
    Copy a Session-view clip into the Arrangement timeline.

    Uses Live's track.duplicate_clip_to_arrangement() API (Live 11 / 12).
    The clip is placed at destination_time beats from the start of the
    arrangement on the same track it lives in.

    Typical workflow:
      1. create_clip / add_notes_to_clip to build a Session clip
      2. Call duplicate_to_arrangement once per bar/section you need
      3. Call switch_to_arrangement_view to confirm the result in Live

    Parameters:
    - track_index:       Index of the track that owns the Session clip
    - clip_index:        Index of the clip slot in that track (Session view)
    - destination_time:  Beat position in the arrangement to place the clip
                         (e.g. 0.0 = start, 8.0 = bar 3 in 4/4)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command(
            "duplicate_session_clip_to_arrangement",
            {
                "track_index": track_index,
                "clip_index": clip_index,
                "destination_time": destination_time
            }
        )
        clip_name = result.get("clip_name", "clip")
        track_name = result.get("track_name", f"track {track_index}")
        return (
            f"Duplicated '{clip_name}' from Session slot {clip_index} "
            f"on '{track_name}' to arrangement at beat {destination_time}"
        )
    except Exception as e:
        logger.error(f"Error duplicating clip to arrangement: {str(e)}")
        return f"Error duplicating clip to arrangement: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("create_locator")
def create_locator(
    ctx: Context,
    name: str,
    time: float,
    user_prompt: str = ""
) -> str:
    """
    Create a named locator (cue point) in the Arrangement at a beat position.

    If a locator already exists at that beat (within ~1e-3 tolerance) it is
    renamed instead of toggled off. Time is in beats from the start of the
    arrangement (e.g. 0.0 = start, 16.0 = bar 5 in 4/4).

    Parameters:
    - name: The locator label (e.g. "Chorus", "Verse 1", "Drop")
    - time: Beat position where the locator should sit
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        missing = require_capability("create_locator")
        if missing:
            return missing
        ableton = get_ableton_connection()
        result = ableton.send_command(
            "create_locator",
            {"name": name, "time": time}
        )
        return (
            f"Locator '{result.get('name', name)}' set at beat "
            f"{result.get('time', time)}"
        )
    except Exception as e:
        logger.error(f"Error creating locator: {str(e)}")
        return f"Error creating locator: {str(e)}"


# ── Dataset / preference tools (Supabase trajectory recording) ─────────────────

@mcp.tool()
@telemetry_tool("submit_intent")
@trajectory_tool("submit_intent", modifying=False)
def submit_intent(
    ctx: Context,
    text: str,
    level: int = 5,
    user_prompt: str = "",
) -> str:
    """
    Record the human's creative intent for subsequent actions in this session.

    Call this before a sequence of edits so trajectory steps are conditioned on intent.
    Levels: 1=atomic, 2=musical op, 3=section, 4=song, 5=creative goal.

    Requires telemetry consent. Data is stored in Supabase (same project as telemetry).

    Parameters:
    - text: Natural-language intent (e.g. "make the chorus feel bigger")
    - level: Hierarchical intent level 1–5 (default 5)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    recorder = get_recorder()
    if recorder is None:
        return (
            "Dataset recording is off (telemetry disabled or user has not consented)."
        )
    try:
        event = recorder.set_intent(text=text, level=level)
        return (
            f"Recorded intent {event.intent_id} (level {level}): {text!r}. "
            f"Session: {recorder.session_id}"
        )
    except Exception as e:
        logger.error(f"Error submitting intent: {str(e)}")
        return f"Error submitting intent: {str(e)}"


@mcp.tool()
@telemetry_tool("rate_last_action")
@trajectory_tool("rate_last_action", modifying=False)
def rate_last_action(
    ctx: Context,
    rating: str,
    tags: str = "",
    note: str = "",
    user_prompt: str = "",
) -> str:
    """
    Rate the most recent recorded action (or the track after an edit).

    Ratings: better | same | worse | keep | reject | thumbs_up | thumbs_down
    Optional tags (comma-separated): groove, harmony, melody, sound, arrangement,
    energy, mix, emotion

    Requires telemetry consent.

    Parameters:
    - rating: Preference label
    - tags: Comma-separated aspect tags
    - note: Optional free-text reason
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    recorder = get_recorder()
    if recorder is None:
        return (
            "Dataset recording is off (telemetry disabled or user has not consented)."
        )
    try:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
        event = recorder.record_preference(
            rating=rating.strip().lower(),
            tags=tag_list,
            note=note or None,
        )
        target = event.target_action_id or "none"
        return f"Recorded preference {rating!r} for action {target}"
    except Exception as e:
        logger.error(f"Error rating action: {str(e)}")
        return f"Error rating action: {str(e)}"


@mcp.tool()
@telemetry_tool("prefer_candidate")
@trajectory_tool("prefer_candidate", modifying=False)
def prefer_candidate(
    ctx: Context,
    candidate_a: str,
    candidate_b: str,
    winner: str,
    reason: str = "",
    user_prompt: str = "",
) -> str:
    """
    Record a pairwise preference between two candidate actions or auditions.

    winner should be 'a', 'b', or an explicit candidate id matching A or B.

    Requires telemetry consent.

    Parameters:
    - candidate_a: Id / URI / label for option A
    - candidate_b: Id / URI / label for option B
    - winner: 'a', 'b', or the winning id
    - reason: Optional reason ("C has the right attack")
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    recorder = get_recorder()
    if recorder is None:
        return (
            "Dataset recording is off (telemetry disabled or user has not consented)."
        )
    try:
        w = winner.strip().lower()
        if w == "a":
            w = candidate_a
        elif w == "b":
            w = candidate_b
        event = recorder.record_preference(
            rating="pairwise",
            winner=w,
            candidate_a=candidate_a,
            candidate_b=candidate_b,
            note=reason or None,
        )
        return f"Recorded pairwise preference: winner={w!r} (event {event.event_id})"
    except Exception as e:
        logger.error(f"Error recording pairwise preference: {str(e)}")
        return f"Error recording pairwise preference: {str(e)}"


@mcp.tool()
@telemetry_tool("reject_last_action")
@trajectory_tool("reject_last_action", modifying=False)
def reject_last_action(
    ctx: Context,
    reason: str = "",
    user_prompt: str = "",
) -> str:
    """
    Mark the last action as rejected (exploration / preference boundary).

    Use when the human undoes or discards an agent edit. Requires telemetry consent.

    Parameters:
    - reason: Optional why it was rejected
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    recorder = get_recorder()
    if recorder is None:
        return (
            "Dataset recording is off (telemetry disabled or user has not consented)."
        )
    try:
        event = recorder.record_preference(
            rating="reject",
            note=reason or None,
        )
        return f"Recorded rejection for action {event.target_action_id or 'none'}"
    except Exception as e:
        logger.error(f"Error rejecting action: {str(e)}")
        return f"Error rejecting action: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("record_audition")
@trajectory_tool("record_audition", modifying=False)
def record_audition(
    ctx: Context,
    uri: str,
    kept: bool = False,
    search_query: str = "",
    dwell_ms: float = 0.0,
    user_prompt: str = "",
) -> str:
    """
    Log a browser/preset/sample audition (keep or reject) for preference learning.

    Call once per candidate auditioned. Does not load the device — use
    load_instrument_or_effect when kept=True and you want to commit.

    Requires telemetry consent.

    Parameters:
    - uri: Browser item URI (or stable preset/sample id)
    - kept: Whether this candidate was kept
    - search_query: Optional search text that led here (e.g. "analog bass")
    - dwell_ms: Optional time spent auditioning
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    recorder = get_recorder()
    if recorder is None:
        return (
            "Dataset recording is off (telemetry disabled or user has not consented)."
        )
    try:
        event = recorder.record_audition(
            uri=uri,
            kept=kept,
            search_query=search_query or None,
            dwell_ms=dwell_ms if dwell_ms > 0 else None,
        )
        status = "kept" if kept else "rejected"
        return f"Recorded audition ({status}) uri={uri!r} event={event.event_id}"
    except Exception as e:
        logger.error(f"Error recording audition: {str(e)}")
        return f"Error recording audition: {str(e)}"



# ------------------------------------------------------------- MCP-15
# Suno bridge tools. The logic lives in MCP_Server.suno_bridge, the
# MCP client in MCP_Server.suno_client, and the fork's own Suno OAuth
# grant in MCP_Server.suno_auth; everything is imported lazily inside
# _get_suno_bridge so importing this module (and the lean profile's
# no-side-plane rule) is untouched. No Suno call happens at import
# time, and no Live command is sent by the read-only tools.


class _SunoLivePlacer:
    """The Live edge of the Suno bridge: place a downloaded file on a
    fresh audio track and prove it landed by reading the track back.

    Placement is verified, never assumed: if slot 0 of the new track
    does not read back with a clip, the placement raises.
    """

    def __init__(self, connection_factory):
        self._connection_factory = connection_factory

    def _conn(self):
        return self._connection_factory()

    def get_clip_slot(self, track_index: int, clip_index: int):
        result = self._conn().send_command(
            "get_track_info", {"track_index": track_index}
        )
        for slot in result.get("clip_slots") or []:
            if slot.get("index") == clip_index:
                return {
                    "has_clip": bool(slot.get("has_clip")),
                    "clip": slot.get("clip"),
                }
        return None

    def place_audio_file(self, path: str, track_name: str,
                         tempo_bpm: float = None) -> dict:
        conn = self._conn()
        before = conn.send_command("get_session_info")
        created = conn.send_command("create_audio_track", {"index": -1})
        track_index = created.get("index")
        if track_index is None:
            tracks = before.get("tracks") or []
            track_index = before.get("track_count", len(tracks))
        conn.send_command(
            "set_track_name",
            {"track_index": track_index, "name": track_name},
        )
        clip_result = conn.send_command(
            "create_audio_clip",
            {
                "track_index": track_index,
                "clip_index": 0,
                "path": str(path),
            },
        )
        tempo_set = None
        if tempo_bpm:
            conn.send_command("set_tempo", {"tempo": float(tempo_bpm)})
            tempo_set = float(tempo_bpm)
        verify = conn.send_command(
            "get_track_info", {"track_index": track_index}
        )
        placed_clip = None
        for slot in verify.get("clip_slots") or []:
            if slot.get("index") == 0:
                placed_clip = slot.get("clip")
        if not placed_clip:
            raise Exception(
                "Placement verification failed: no clip read back in "
                "slot 0 of track %s after create_audio_clip"
                % (track_index,)
            )
        return {
            "track_index": track_index,
            "clip_index": 0,
            "track_name": track_name,
            "clip": placed_clip,
            "clip_result": clip_result,
            "tempo_set": tempo_set,
        }


_suno_bridge = None


def _get_suno_bridge():
    """Build the bridge on first use (never at import time)."""
    global _suno_bridge
    if _suno_bridge is None:
        from pathlib import Path

        from .suno_auth import build_client
        from .suno_bridge import SunoBridge

        client, _oauth = build_client()
        download_dir = os.environ.get(
            "ABLETON_MCP_SUNO_DOWNLOAD_DIR"
        ) or str(Path.home() / "Downloads" / "AbletonMCP-Suno")
        _suno_bridge = SunoBridge(
            client,
            _SunoLivePlacer(get_ableton_connection),
            download_dir,
        )
    return _suno_bridge


def _suno_text(value: str):
    """Tool arguments arrive as strings; empty means 'not given'."""
    return value if value else None


@mcp.tool()
@telemetry_tool("suno_status")
@trajectory_tool("suno_status", modifying=False)
def suno_status(ctx: Context, user_prompt: str = "") -> str:
    """
    Read the Suno account and usage for the bridge's own sign-in.

    Returns the signed-in profile, the plan, the remaining credits
    and the allowed models/features. Read-only: spends nothing. This
    is the pre-generation guard surface - suno_generate checks the
    same usage before creating anything.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        return _dump_tool_result(_get_suno_bridge().status())
    except Exception as e:
        logger.error(f"Error reading Suno status: {str(e)}")
        return f"Error reading Suno status: {str(e)}"


@mcp.tool()
@telemetry_tool("suno_generate")
@trajectory_tool("suno_generate")
def suno_generate(
    ctx: Context,
    description: str = "",
    lyrics: str = "",
    style: str = "",
    title: str = "",
    instrumental: bool = False,
    duration: int = 0,
    duration_strict: bool = False,
    model: str = "",
    clip_count: int = 1,
    wait_budget_seconds: int = 600,
    user_prompt: str = "",
) -> str:
    """
    Generate audio on Suno and wait for it to finish (no Live placement).

    Simple mode: pass description alone. Custom mode: pass lyrics,
    usually with style and title. The modes cannot be mixed. Costs
    Suno credits: the account usage is checked first and the call is
    refused when the remaining credits cannot cover clip_count.
    clip_count is pinned explicitly (Suno defaults an MCP sign-in to
    2); it must be 1 or 2. duration is a target length in seconds
    (15-360, 0 = model chooses); duration_strict is sent explicitly
    whenever a duration is given. If the wait budget runs out first,
    the result reports status "pending" with the live clip ids -
    nothing is re-submitted.

    Parameters:
    - description: Natural-language song description (Simple mode)
    - lyrics: Lyrics to sing (Custom mode)
    - style: Style tags (Custom mode)
    - title: Song title (Custom mode)
    - instrumental: Force a vocal-free generation
    - duration: Target length in seconds, 0 = model chooses
    - duration_strict: Hard-cap the clip at duration
    - model: Suno model label, empty = plan default
    - clip_count: Songs per generation, 1 or 2 (default 1)
    - wait_budget_seconds: Total seconds to wait for completion
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        result = _get_suno_bridge().generate(
            description=_suno_text(description),
            lyrics=_suno_text(lyrics),
            style=_suno_text(style),
            title=_suno_text(title),
            instrumental=instrumental,
            duration=duration if duration else None,
            duration_strict=duration_strict,
            model=_suno_text(model),
            clip_count=clip_count,
            wait_budget_seconds=wait_budget_seconds,
        )
        return _dump_tool_result(result)
    except Exception as e:
        logger.error(f"Error generating on Suno: {str(e)}")
        return f"Error generating on Suno: {str(e)}"


@mcp.tool()
@telemetry_tool("suno_generate_to_live")
@trajectory_tool("suno_generate_to_live")
def suno_generate_to_live(
    ctx: Context,
    description: str = "",
    lyrics: str = "",
    style: str = "",
    title: str = "",
    instrumental: bool = False,
    duration: int = 0,
    duration_strict: bool = False,
    model: str = "",
    clip_count: int = 1,
    match_tempo: bool = False,
    allow_download_credit: bool = False,
    wait_budget_seconds: int = 600,
    user_prompt: str = "",
) -> str:
    """
    Generate on Suno, download the WAV, and place it in Live.

    The full generate flow of suno_generate, then: Suno's audio
    analysis is read best-effort (bpm/key when it is ready), the WAV
    is downloaded, and the file is placed on a new audio track named
    "<title> [suno:<clip_id>]", verified by reading the track back.
    A sidecar .suno.json mapping file is written next to the WAV.
    The download follows Suno's credit rule: if it needs a download
    credit and allow_download_credit is False, the tool stops and
    reports "requires_download_credit" instead of spending it.
    match_tempo sets Live's tempo to the analysed bpm - only when an
    analysis bpm was actually measured. With clip_count 2 only the
    first finished song is placed; both ids are in the result.

    Parameters:
    - description: Natural-language song description (Simple mode)
    - lyrics: Lyrics to sing (Custom mode)
    - style: Style tags (Custom mode)
    - title: Song title (Custom mode)
    - instrumental: Force a vocal-free generation
    - duration: Target length in seconds, 0 = model chooses
    - duration_strict: Hard-cap the clip at duration
    - model: Suno model label, empty = plan default
    - clip_count: Songs per generation, 1 or 2 (default 1)
    - match_tempo: Set Live's tempo to the analysed bpm
    - allow_download_credit: Approve spending a download credit if required
    - wait_budget_seconds: Total seconds to wait for completion
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        result = _get_suno_bridge().generate_to_live(
            description=_suno_text(description),
            lyrics=_suno_text(lyrics),
            style=_suno_text(style),
            title=_suno_text(title),
            instrumental=instrumental,
            duration=duration if duration else None,
            duration_strict=duration_strict,
            model=_suno_text(model),
            clip_count=clip_count,
            match_tempo=match_tempo,
            allow_download_credit=allow_download_credit,
            wait_budget_seconds=wait_budget_seconds,
        )
        return _dump_tool_result(result)
    except Exception as e:
        logger.error(f"Error generating to Live from Suno: {str(e)}")
        return f"Error generating to Live from Suno: {str(e)}"


@mcp.tool()
@telemetry_tool("suno_library_to_live")
@trajectory_tool("suno_library_to_live")
def suno_library_to_live(
    ctx: Context,
    clip_id: str = "",
    query: str = "",
    match_tempo: bool = False,
    allow_download_credit: bool = False,
    user_prompt: str = "",
) -> str:
    """
    Place an existing song from the Suno library into Live.

    No generation cost. Pass clip_id for an exact song, or query to
    take the newest library song whose title/style matches, or
    neither for the newest library song. Download, analysis and
    placement follow suno_generate_to_live, including the download
    credit rule (allow_download_credit) and tempo matching
    (match_tempo).

    Parameters:
    - clip_id: Exact Suno clip id to place
    - query: Library search text when clip_id is empty
    - match_tempo: Set Live's tempo to the analysed bpm
    - allow_download_credit: Approve spending a download credit if required
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        result = _get_suno_bridge().library_to_live(
            clip_id=_suno_text(clip_id),
            query=_suno_text(query),
            match_tempo=match_tempo,
            allow_download_credit=allow_download_credit,
        )
        return _dump_tool_result(result)
    except Exception as e:
        logger.error(f"Error placing Suno library song in Live: {str(e)}")
        return f"Error placing Suno library song in Live: {str(e)}"


@mcp.tool()
@telemetry_tool("suno_cover_clip")
@trajectory_tool("suno_cover_clip")
def suno_cover_clip(
    ctx: Context,
    track_index: int,
    clip_index: int,
    file_path: str,
    style: str = "",
    lyrics: str = "",
    keep_vocals: bool = False,
    voice_id: str = "",
    title: str = "",
    clip_count: int = 1,
    match_tempo: bool = False,
    allow_download_credit: bool = False,
    wait_budget_seconds: int = 600,
    user_prompt: str = "",
) -> str:
    """
    Cover the audio clip in a Live clip slot with Suno, place the result back.

    The clip slot must hold a clip (checked against Live). file_path
    is the clip's underlying audio file on disk - the frozen Live
    wire surface does not expose clip file paths, so the caller
    supplies it; v1 uploads that file as-is (a warped clip's rendered
    sound may differ - a rendered bounce is v2). The upload uses
    Suno's PUT flow, then the cover is created. Suno does NOT carry
    a source's lyrics into a cover: with keep_vocals and no new
    lyrics, the uploaded clip's own lyrics are passed explicitly so
    the vocals survive; with neither, the cover is instrumental
    unless voice_id is set. Costs Suno credits (usage checked first).

    Parameters:
    - track_index: Live track holding the source clip
    - clip_index: Clip slot holding the source clip
    - file_path: Absolute path to the clip's audio file on disk
    - style: New style for the cover
    - lyrics: New lyrics, empty = keep source lyrics when keep_vocals
    - keep_vocals: Pass the source lyrics explicitly to keep vocals
    - voice_id: Suno voice id to sing the cover
    - title: Title for the cover
    - clip_count: Covers per call, 1 or 2 (default 1)
    - match_tempo: Set Live's tempo to the analysed bpm of the result
    - allow_download_credit: Approve spending a download credit if required
    - wait_budget_seconds: Total seconds to wait per stage
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        result = _get_suno_bridge().cover_clip(
            track_index=track_index,
            clip_index=clip_index,
            file_path=file_path,
            style=_suno_text(style),
            lyrics=_suno_text(lyrics),
            keep_vocals=keep_vocals,
            voice_id=_suno_text(voice_id),
            title=_suno_text(title),
            clip_count=clip_count,
            match_tempo=match_tempo,
            allow_download_credit=allow_download_credit,
            wait_budget_seconds=wait_budget_seconds,
        )
        return _dump_tool_result(result)
    except Exception as e:
        logger.error(f"Error covering clip with Suno: {str(e)}")
        return f"Error covering clip with Suno: {str(e)}"


@mcp.tool()
@telemetry_tool("suno_extend_clip")
@trajectory_tool("suno_extend_clip")
def suno_extend_clip(
    ctx: Context,
    track_index: int,
    clip_index: int,
    file_path: str,
    continue_at: float,
    lyrics: str = "",
    style: str = "",
    title: str = "",
    clip_count: int = 1,
    match_tempo: bool = False,
    allow_download_credit: bool = False,
    wait_budget_seconds: int = 600,
    user_prompt: str = "",
) -> str:
    """
    Extend the audio clip in a Live clip slot with Suno, place the whole song back.

    Like suno_cover_clip, the slot must hold a clip and file_path is
    the clip's underlying audio file (v1 uploads it as-is). The
    extension continues the source from continue_at seconds and
    contains only the continuation; the bridge joins the chain with
    Suno's full-song operation (free, idempotent) and places the
    joined song. Costs Suno credits (usage checked first).

    Parameters:
    - track_index: Live track holding the source clip
    - clip_index: Clip slot holding the source clip
    - file_path: Absolute path to the clip's audio file on disk
    - continue_at: Seconds into the source clip to continue from
    - lyrics: Lyrics for the continuation
    - style: Style tags for the continuation
    - title: Title for the extended song
    - clip_count: Extensions per call, 1 or 2 (default 1)
    - match_tempo: Set Live's tempo to the analysed bpm of the result
    - allow_download_credit: Approve spending a download credit if required
    - wait_budget_seconds: Total seconds to wait per stage
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        result = _get_suno_bridge().extend_clip(
            track_index=track_index,
            clip_index=clip_index,
            file_path=file_path,
            continue_at=continue_at,
            lyrics=_suno_text(lyrics),
            style=_suno_text(style),
            title=_suno_text(title),
            clip_count=clip_count,
            match_tempo=match_tempo,
            allow_download_credit=allow_download_credit,
            wait_budget_seconds=wait_budget_seconds,
        )
        return _dump_tool_result(result)
    except Exception as e:
        logger.error(f"Error extending clip with Suno: {str(e)}")
        return f"Error extending clip with Suno: {str(e)}"



# Main execution
def main():
    """Run the MCP server"""
    mcp.run()

if __name__ == "__main__":
    main()