# AbletonMCP/init.py
from __future__ import absolute_import, print_function, unicode_literals

from _Framework.ControlSurface import ControlSurface
import os
import socket
import json
import threading
import time
import traceback
import struct
from collections import OrderedDict, deque

# Change queue import for Python 2
try:
    import Queue as queue  # Python 2
except ImportError:
    import queue  # Python 3

# Constants for socket communication
DEFAULT_PORT = 9877
# Loopback, not 0.0.0.0. This socket takes unauthenticated commands that import
# arbitrary absolute paths, load browser items and drive the transport, so
# anything that can reach the port controls the DAW. Binding every interface
# exposed that to the whole local network — and to any network the machine is
# routable from.
#
# Running Live and the MCP server on different machines is the one legitimate
# reason to widen this; set ABLETON_MCP_HOST to do so deliberately, and put it
# behind a trusted network or an explicit relay.
HOST = os.environ.get("ABLETON_MCP_HOST", "127.0.0.1")

# A command that never completes must not grow the buffer without bound.
# Generous enough for a large add_notes_to_clip payload.
MAX_REQUEST_BYTES = 16 * 1024 * 1024

# Bumped whenever the TCP command surface changes; the MCP server compares
# this to EXPECTED_REMOTE_SCRIPT_VERSION.
SCRIPT_VERSION = "1.7.10"
PROTOCOL_VERSION = 1

# MCP-10: the dispatcher table - the single source for the handshake
# capabilities and for each command's thread class.
#
# Thread classes:
#
# * "main": the command's handler touches the Live object model
#   (self._song / self.application(), directly or through a helper),
#   and Live's API is main-thread-only, so _process_command dispatches
#   it through a main-thread scheduling (see _schedule_main_thread_task)
#   and waits on a response queue. Every "main" entry was classified
#   from its handler's code in the MCP-10 audit; read-only is not
#   exempt - a handler that only reads the LOM (inspect_rack) is
#   still a LOM access.
#
# * "handler": the command executes on the calling (socket) thread,
#   with no scheduling. Two populations share this class, and the
#   class claims nothing about the second one:
#   - handlers proven to touch only script-owned state and module
#     constants: get_script_info, drain_passive_events,
#     refresh_browser_cache;
#   - handlers that read the LOM on the calling thread - the
#     upstream dispatcher's long-standing shape, preserved exactly.
#     Their off-thread safety is NOT proven (LOM thread-safety cannot
#     be established from this code), and MCP-10 neither moves nor
#     reclassifies them.
#   get_browser_categories / get_browser_items name handlers that do
#   not exist; their branches raise before any state is touched, and
#   that behaviour is likewise preserved.
#
# SCRIPT_CAPABILITIES is derived from this table, in table order -
# keep the entries sorted, so the advertised list stays byte-identical
# to the historical hand-maintained one. A fork test derives the
# dispatcher's command set from this file's AST and pins the table to
# it, so the handshake can never again advertise a subset of what the
# script implements (it once advertised 19 of 37 commands); a second
# test pins the table's classes against observed dispatch behaviour,
# so class and dispatch cannot drift apart either.
THREAD_MAIN = "main"
THREAD_HANDLER = "handler"

COMMAND_TABLE = (
    ("add_notes_to_clip", THREAD_MAIN),
    ("clear_notes_from_clip", THREAD_MAIN),
    ("create_audio_clip", THREAD_MAIN),
    ("create_audio_track", THREAD_MAIN),
    ("create_clip", THREAD_MAIN),
    ("create_locator", THREAD_MAIN),
    ("create_midi_track", THREAD_MAIN),
    ("delete_clip", THREAD_MAIN),
    ("drain_passive_events", THREAD_HANDLER),
    ("duplicate_session_clip_to_arrangement", THREAD_MAIN),
    ("execute_batch", THREAD_MAIN),
    ("fire_clip", THREAD_MAIN),
    ("get_arrangement_clips", THREAD_HANDLER),
    ("get_browser_categories", THREAD_HANDLER),
    ("get_browser_item", THREAD_HANDLER),
    ("get_browser_items", THREAD_HANDLER),
    ("get_browser_items_at_path", THREAD_HANDLER),
    ("get_browser_tree", THREAD_HANDLER),
    ("get_clip_notes", THREAD_HANDLER),
    ("get_device_parameters", THREAD_HANDLER),
    ("get_script_info", THREAD_HANDLER),
    ("get_session_info", THREAD_HANDLER),
    ("get_session_snapshot", THREAD_HANDLER),
    ("get_track_info", THREAD_HANDLER),
    ("inspect_rack", THREAD_MAIN),
    ("load_browser_item", THREAD_MAIN),
    ("load_instrument_or_effect", THREAD_MAIN),
    ("map_rack_magnitude", THREAD_MAIN),
    ("refresh_browser_cache", THREAD_HANDLER),
    ("search_browser", THREAD_HANDLER),
    ("set_arrangement_clip_name", THREAD_MAIN),
    ("set_clip_name", THREAD_MAIN),
    ("set_current_song_time", THREAD_MAIN),
    ("set_device_parameter", THREAD_MAIN),
    ("set_device_parameters", THREAD_MAIN),
    ("set_passive_listeners", THREAD_MAIN),
    ("set_tempo", THREAD_MAIN),
    ("set_track_name", THREAD_MAIN),
    ("start_playback", THREAD_MAIN),
    ("stop_clip", THREAD_MAIN),
    ("stop_playback", THREAD_MAIN),
    ("switch_to_arrangement_view", THREAD_MAIN),
)

SCRIPT_CAPABILITIES = [name for name, _thread_class in COMMAND_TABLE]

# Thread class by command name, derived from the table.
COMMAND_THREAD_CLASS = dict(COMMAND_TABLE)

# "main" commands discharged by a dedicated branch of _process_command
# instead of the shared scheduled branch below: execute_batch and
# set_device_parameters schedule their own single main-thread task;
# set_device_parameter and set_passive_listeners have their own
# response-queue blocks. Batch rule (MCP-10): a batch is always ONE
# main-thread task, even when every item in it is "handler" class -
# batches are never split by item class.
_BESPOKE_MAIN_THREAD_COMMANDS = frozenset([
    "execute_batch",
    "set_device_parameter",
    "set_device_parameters",
    "set_passive_listeners",
])

# Membership of the shared scheduled branch of _process_command,
# derived from the table - never written out as a literal again.
_SCHEDULED_GROUP_COMMANDS = frozenset(
    name for name, thread_class in COMMAND_TABLE
    if thread_class == THREAD_MAIN) - _BESPOKE_MAIN_THREAD_COMMANDS

# MCP-01 native timing: largest request id the wire's JSON consumers can
# represent exactly (2**53 - 1).
MAX_REQUEST_ID = 9007199254740991

# MCP-02: largest command list execute_batch will run in one scheduling.
# A batch holds Live's main thread for its whole duration, so the list is
# capped far below what the 16 MiB request ceiling alone would allow.
MAX_BATCH_COMMANDS = 1000

# MCP-06: largest parameter list set_device_parameters will apply in
# one scheduling. Each item is a single attribute write, but the list
# still holds Live's main thread for its whole duration, so it is
# capped on the same rationale as MAX_BATCH_COMMANDS.
MAX_BULK_PARAMETERS = 1000

# MCP-07: browser cache bounds. The URI memo and the serialized
# browser-read results live for the script instance's lifetime, so
# both are LRU-bounded and explicitly invalidated (see
# _invalidate_browser_caches) instead of growing forever.
MAX_BROWSER_URI_CACHE_ENTRIES = 512
MAX_BROWSER_READ_CACHE_ENTRIES = 128

# MCP-08: passive-listener diet. The LOM listener census (song, per
# track, per clip slot, per clip) is registered only after a consumer
# enables it via set_passive_listeners; the event queue is a bounded
# deque - drop-oldest with a counted drop total - instead of a list
# whose overflow slice copied the whole queue on every event.
PASSIVE_EVENT_QUEUE_MAX = 500
# Continuous value streams coalesce latest-value-wins per
# (type, track_index, clip_index): a consumer draining after a fader
# drag needs the final value, not every intermediate one. Discrete
# events (names, mute, notes, ...) never coalesce.
PASSIVE_COALESCE_TYPES = frozenset([
    "volume_changed",
    "panning_changed",
    "tempo_changed",
])

# MCP-07: paging bounds for the opt-in get_browser_items_at_path page
# form and for search_browser.
DEFAULT_BROWSER_ITEMS_PAGE_SIZE = 100
MAX_BROWSER_ITEMS_PAGE_SIZE = 1000
DEFAULT_BROWSER_SEARCH_PAGE_SIZE = 50
MAX_BROWSER_SEARCH_PAGE_SIZE = 200

# MCP-03 framing v2: a request (and its response) may travel as one
# length-prefixed frame - 4-byte big-endian payload length + the UTF-8
# JSON payload - instead of v1's raw single JSON object. The payload
# ceiling is MAX_REQUEST_BYTES, the same 16 MiB v1 allows a whole
# request. Support is advertised in the handshake (agent0_framing);
# framing is sniffed at every message boundary and a peer is always
# answered in the framing its message used, so a v1 handshake can be
# followed by framed exchanges on the same connection and v1 clients
# are unaffected. The canonical codec description lives in
# MCP_Server/framing.py.


def _is_valid_request_id(value):
    """A request_id is an integer in [0, MAX_REQUEST_ID]; bools excluded."""
    return type(value) is int and 0 <= value <= MAX_REQUEST_ID


def _coerce_page_int(value, default, maximum=None, minimum=0):
    """Lenient page-parameter coercion (MCP-07), in the style of the
    MCP-06 subset selectors: an unusable value falls back to the
    default instead of failing the whole read; a usable one is
    clamped to [minimum, maximum]. Page sizes pass minimum=1: a
    zero-size page would report a next_offset equal to its own
    offset, so a client paging on next_offset would never advance."""
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        # OverflowError is int(inf) / int(-inf): a non-finite float
        # is unusable like any other, so it falls back too (SI-236).
        return default
    if number < minimum:
        number = minimum
    if maximum is not None and number > maximum:
        number = maximum
    return number


def _require_hashable_cache_key(value, field):
    """Refuse a browser cache-key component that cannot be hashed.

    MCP-07 keys its browser caches on caller-supplied values
    (SI-237): a list or dict used as a key component crashes the
    lookup itself with a raw "unhashable type: ..." interpreter
    message, before the handler's own logic runs. A value that can
    never serve as a cache key is refused by name here instead, in
    the SI-71 entry-refusal style. Hashable values pass through
    untouched - including the legacy hashable non-string scalars
    (a category_type of 42 succeeds softly, a path of 42 fails
    later in its handler exactly as before); only the unhashable
    shapes change.
    """
    try:
        hash(value)
    except TypeError:
        raise ValueError("Invalid {0}: must be a string".format(field))
    return value


def _require_list_selector(value, field):
    """Refuse a subset/projection selector that is not a list/tuple.

    MCP-05/MCP-06 selectors (SI-241): a string selector was iterated
    character-wise ("12" selected indices 1 and 2), a dict selector
    iterated its keys, and a non-iterable selector died with a raw
    "'int' object is not iterable" - all silent or misleading. A
    selector that is not a list/tuple can never be honoured as sent,
    so it is refused by name here, in the SI-237 entry-refusal style.
    Lists and tuples pass through untouched.
    """
    if not isinstance(value, (list, tuple)):
        raise ValueError(
            "Invalid {0}: must be a list or tuple".format(field))
    return value


def _require_index(value, field, count, range_message=None):
    """Refuse an index that is not a real int in range, by name.

    The handler family's copy-pasted guards checked range only
    (``if track_index < 0 or track_index >= len(...)``) (SI-242):
    a bool index silently selected element 0/1 (``track_index=True``
    read and wrote track 1 and echoed ``"index": true`` in the
    result), a NaN or integral-float index passed both comparisons
    and died at indexing with a raw ``list indices must be integers
    or slices, not float``, and string/list/None/tuple/bytes indices
    died at the comparison itself with a raw ``'<' not supported
    between instances of ...``. A value that can never address an
    element is refused by name here instead, in the SI-237
    entry-refusal style. The type check is ``type(value) is int`` -
    bools excluded - exactly as ``_is_valid_request_id`` and the
    SI-241 selector entry rule already require. A real int outside
    ``[0, count)`` raises the call site's existing IndexError
    message unchanged, so out-of-range behaviour (including -1
    refusing rather than wrapping) is byte-identical to the old
    guard, and a valid int passes through untouched.

    ``count`` may be None for an index no Python collection bounds:
    the create-track position, consumed by the LOM call
    ``Song.create_midi_track(index)`` where -1 means "at the end".
    Only the type is validated there; every int keeps the meaning
    it has today.
    """
    if type(value) is not int:
        raise ValueError(
            "Invalid {0}: must be an integer".format(field))
    if count is not None and (value < 0 or value >= count):
        raise IndexError(range_message)
    return value


def _require_finite_number(value, field):
    """Refuse a value that is not a finite real number, by name.

    The value-taking handlers forwarded caller values to the Live
    object model unchecked (SI-243 / SI-239 F4): set_tempo assigned
    NaN, "fast", [120], None and True to Song.tempo verbatim and
    reported success; set_device_parameter stored NaN and silently
    clamped +/-Infinity to the parameter's max/min; the float()-
    converted time handlers accepted NaN/+/-Infinity and numeric
    strings while None/list died with raw float() interpreter
    messages; create_clip forwarded its length verbatim. A value
    that is not a finite number can never be honoured as sent, so
    it is refused by name here instead, in the SI-237 entry-refusal
    style.

    Only real int/float values pass: bools are excluded (a bool is
    not a number for value purposes - the SI-242 index rule), and
    numeric strings are NOT coerced ("0.5" refuses, it does not
    become 0.5 - the SI-241 digit-string non-coercion ruling for
    selector entries, applied to values). A passing int is returned
    as its float, exactly what the handlers' own float() conversion
    produced for every valid value before this row; a passing float
    is returned untouched.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            "Invalid {0}: must be a finite number".format(field))
    try:
        number = float(value)
    except OverflowError:
        raise ValueError(
            "Invalid {0}: must be a finite number".format(field))
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(
            "Invalid {0}: must be a finite number".format(field))
    return number


def _require_string(value, field):
    """Refuse a value that is not a string, by name.

    The name setters assigned caller values verbatim (SI-243 /
    SI-239 F4): set_track_name stored a list or None as the track
    name and echoed it back as success, and create_locator silently
    stringified a list name to "['x']". A non-string name can never
    be honoured as sent, so it is refused by name here instead, in
    the SI-237 entry-refusal style - the same isinstance(value, str)
    check the SI-241 selector name entries use. Strings pass through
    untouched, the empty string included.
    """
    if not isinstance(value, str):
        raise ValueError("Invalid {0}: must be a string".format(field))
    return value


def _require_bool(value, field):
    """Refuse a value that is not a real bool, by name.

    Boolean fields read by truthiness take the TRUE branch for the
    string "false" - the most likely wrong-type bool a JSON caller
    sends (SI-244, for the note ``mute`` field; the wider
    truthiness family is SI-239 F6). A non-bool mute forwarded to
    Clip.set_notes can never be honoured as sent, so it is refused
    by name here instead, in the SI-237 entry-refusal style. Real
    bools pass through untouched. SI-245 reuses this helper for the
    rest of the F6 family: the dispatcher ``timing`` opt-in, the
    ``set_passive_listeners`` ``enabled`` flag, both
    ``stop_on_error`` flags, and the MCP-05 projection
    ``include_*`` flags.
    """
    if not isinstance(value, bool):
        raise ValueError(
            "Invalid {0}: must be a boolean".format(field))
    return value


def _finish_agent0_timing(response, timing_state):
    """Attach the agent0_timing block to a response envelope.

    dispatch_ms: _process_command entry -> response ready (excludes socket
    receive and response send, as in the original host-patch design).
    queue_ms: dispatch entry -> main-thread task start; None for commands
    that run directly on the socket thread.
    execution_ms: main-thread task start -> finish; None likewise.
    """
    finished = time.perf_counter()
    started = timing_state["started"]
    main_started = timing_state.get("main_started")
    main_finished = timing_state.get("main_finished")
    response["agent0_timing"] = {
        "dispatch_ms": (finished - started) * 1000.0,
        "queue_ms": ((main_started - started) * 1000.0
                     if main_started is not None else None),
        "execution_ms": ((main_finished - main_started) * 1000.0
                         if main_started is not None
                         and main_finished is not None else None),
    }
    if timing_state.get("request_id") is not None:
        response["request_id"] = timing_state["request_id"]

def _encode_frame(payload):
    """Prefix *payload* (bytes) with its length for framing v2.

    Wire format: 4-byte big-endian payload length + the payload
    itself. Shared with MCP_Server/framing.py - the script cannot
    import the package, so the codec lives here in miniature and the
    two must never drift apart in what they accept.
    """
    return struct.pack(">I", len(payload)) + payload


class _FrameDecoder(object):
    """Incremental framing-v2 decoder for one connection.

    feed() appends received bytes; next_frame() pops one complete
    frame payload, or returns None while only a partial frame is
    buffered. Consumed bytes are removed from the front of the buffer
    and each payload is extracted exactly once - the accumulated
    buffer is never re-parsed - which is the whole point of MCP-03
    (the audit measured v1's re-parse loop at ~61x the one-shot cost
    at 1 MiB and ~133x at 2 MiB). Raises ValueError on a declared
    length outside [2, MAX_REQUEST_BYTES]: after that the stream
    cannot be resynchronised, so the caller reports it and closes.
    """

    def __init__(self):
        self._buffer = bytearray()

    @property
    def pending(self):
        """Bytes held toward an incomplete frame."""
        return len(self._buffer)

    def feed(self, data):
        self._buffer += data

    def next_frame(self):
        if len(self._buffer) < 4:
            return None
        (length,) = struct.unpack(">I", bytes(self._buffer[:4]))
        if length < 2 or length > MAX_REQUEST_BYTES:
            raise ValueError(
                "Invalid frame length %d (framing v2)" % length)
        if len(self._buffer) < 4 + length:
            return None
        payload = bytes(self._buffer[4:4 + length])
        del self._buffer[:4 + length]
        return payload


def create_instance(c_instance):
    """Create and return the AbletonMCP script instance"""
    return AbletonMCP(c_instance)

class AbletonMCP(ControlSurface):
    """AbletonMCP Remote Script for Ableton Live"""
    
    def __init__(self, c_instance):
        """Initialize the control surface"""
        ControlSurface.__init__(self, c_instance)
        self.log_message(
            "AbletonMCP Remote Script initializing... (script v%s)"
            % SCRIPT_VERSION
        )
        
        # Socket server for communication
        self.server = None
        self.client_threads = []
        self.server_thread = None
        self.running = False

        # Per-thread dispatch state (MCP-02). An execute_batch task sets
        # on_main_thread on the thread it runs on - Live's main thread -
        # so the commands it dispatches run inline instead of each
        # scheduling their own main-thread hop (see _execute_batch).
        self._dispatch_state = threading.local()
        
        # Cache the song reference for easier access
        self._song = self.song()

        # Passive human-UI event queue (drained by MCP → Supabase).
        # MCP-08: listeners are NOT registered here - a consumer
        # enables them with set_passive_listeners (the MCP dataset
        # poller does so while dataset consent is on). Until then the
        # script carries no LOM listener overhead at all.
        self._passive_events = deque()
        self._passive_lock = threading.Lock()
        self._passive_max = PASSIVE_EVENT_QUEUE_MAX
        self._passive_dropped = 0
        self._passive_listeners_enabled = False
        self._passive_track_count = None
        self._passive_track_bindings = []  # (track, [(add_name, callback), ...]) for cleanup
        self._passive_clip_bindings = {}  # (track_index, clip_index) -> (clip, [(add_name, callback), ...])
        self._song_passive_callbacks = []
        
        # Start the socket server
        self.start_server()

        # MCP-08: no passive LOM listeners are registered at startup.
        # Registration happens only on set_passive_listeners (see
        # _set_passive_listeners), negotiated per session.
        
        self.log_message("AbletonMCP initialized")
        
        # Show a message in Ableton
        self.show_message("AbletonMCP: Listening for commands on port " + str(DEFAULT_PORT))
    
    def disconnect(self):
        """Called when Ableton closes or the control surface is removed"""
        self.log_message("AbletonMCP disconnecting...")
        self.running = False

        try:
            self._teardown_passive_listeners()
        except Exception as e:
            self.log_message("Passive listener teardown error: " + str(e))
        
        # Stop the server
        if self.server:
            try:
                self.server.close()
            except:
                pass
        
        # Wait for the server thread to exit
        if self.server_thread and self.server_thread.is_alive():
            self.server_thread.join(1.0)
            
        # Clean up any client threads
        for client_thread in self.client_threads[:]:
            if client_thread.is_alive():
                # We don't join them as they might be stuck
                self.log_message("Client thread still alive during disconnect")
        
        ControlSurface.disconnect(self)
        self.log_message("AbletonMCP disconnected")
    
    def start_server(self):
        """Start the socket server in a separate thread"""
        try:
            self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server.bind((HOST, DEFAULT_PORT))
            self.server.listen(5)  # Allow up to 5 pending connections
            
            self.running = True
            self.server_thread = threading.Thread(target=self._server_thread)
            self.server_thread.daemon = True
            self.server_thread.start()
            
            self.log_message("Server started on port " + str(DEFAULT_PORT))
        except Exception as e:
            self.log_message("Error starting server: " + str(e))
            self.show_message("AbletonMCP: Error starting server - " + str(e))
    
    def _server_thread(self):
        """Server thread implementation - handles client connections"""
        try:
            self.log_message("Server thread started")
            # Set a timeout to allow regular checking of running flag
            self.server.settimeout(1.0)
            
            while self.running:
                try:
                    # Accept connections with timeout
                    client, address = self.server.accept()
                    self.log_message("Connection accepted from " + str(address))
                    self.show_message("AbletonMCP: Client connected")
                    
                    # Handle client in a separate thread
                    client_thread = threading.Thread(
                        target=self._handle_client,
                        args=(client,)
                    )
                    client_thread.daemon = True
                    client_thread.start()
                    
                    # Keep track of client threads
                    self.client_threads.append(client_thread)
                    
                    # Clean up finished client threads
                    self.client_threads = [t for t in self.client_threads if t.is_alive()]
                    
                except socket.timeout:
                    # No connection yet, just continue
                    continue
                except Exception as e:
                    if self.running:  # Only log if still running
                        self.log_message("Server accept error: " + str(e))
                    time.sleep(0.5)
            
            self.log_message("Server thread stopped")
        except Exception as e:
            self.log_message("Server thread error: " + str(e))
    
    def _handle_client(self, client):
        """Handle communication with a connected client.

        MCP-03: framing is a property of each message, decided at
        every message boundary from the next bytes on the stream:
        * v1 - the historical raw single JSON object, answered with a
          raw JSON object;
        * v2 - one length-prefixed frame (see _FrameDecoder),
          answered with a frame.
        A peer is always answered in the framing its message used.
        The natural session shape is a v1 handshake - get_script_info
        is the exchange that advertises v2 - followed by framed
        exchanges on the same connection, but the boundary rule is
        uniform, and a v1 client talking to this script sees v1
        unchanged. Bytes that start neither protocol end the
        connection: before any message they close it without a reply
        (there is no framing in which an error could be sent); after
        an exchange they first draw an error reply in that exchange's
        framing, which the handler still holds.
        """
        self.log_message("Client handler started")
        client.settimeout(None)  # No timeout for client socket
        # Accumulate raw bytes, never per-chunk text. A multi-byte UTF-8
        # character (an accented clip name, an emoji) can straddle two recv()
        # boundaries; decoding each chunk on its own raises there, and the old
        # code answered that with an error frame pushed into a stream the
        # client was still reading a real response from — desynchronising the
        # connection for good.
        buffer = bytearray()  # Received, not yet attributed to a message
        sniff_offset = 0  # Buffer bytes the framing sniff already scanned:
        # the leading whitespace is proven, and the first non-whitespace
        # byte (once one is seen) sits at this index. Rescanning from
        # byte 0 on every recv() made a padded request quadratic (SI-72).
        decoder = _FrameDecoder()  # Holds at most one partial v2 frame
        framing = None  # Framing of the message currently being answered

        def send_response(response):
            payload = json.dumps(response).encode('utf-8')
            if framing == "v2":
                client.sendall(_encode_frame(payload))
            else:
                client.sendall(payload)

        def dispatch(command):
            self.log_message(
                "Received command: " + str(command.get("type", "unknown")))
            # Process the command and send its response, written in the
            # framing the command arrived in.
            send_response(self._process_command(command))

        try:
            while self.running:
                try:
                    # Receive data
                    data = client.recv(8192)

                    if not data:
                        # Client disconnected
                        self.log_message("Client disconnected")
                        break

                    # A frame in flight owns the stream until it is
                    # whole; anything else accumulates unattributed.
                    if decoder.pending:
                        decoder.feed(data)
                    else:
                        buffer += data

                    # Answer every message the stream now holds whole.
                    # One recv() may complete a frame, complete a v1
                    # command, or carry several framed messages.
                    while True:
                        if decoder.pending:
                            payload = decoder.next_frame()
                            if payload is None:
                                break  # Partial frame: receive more
                            framing = "v2"
                            command = json.loads(payload.decode('utf-8'))
                            if not isinstance(command, dict):
                                raise ValueError(
                                    "Framing v2 payload is not a JSON object")
                            dispatch(command)
                            continue
                        if not buffer:
                            break  # Nothing attributed: receive more
                        framing = self._sniff_framing(buffer, sniff_offset)
                        if framing is None:
                            if len(buffer) > MAX_REQUEST_BYTES:
                                # The ceiling applies to the unattributed
                                # buffer even when no framing can be decided
                                # yet: an all-whitespace buffer is a v1
                                # request in progress (v1 tolerates leading
                                # whitespace), so refuse it exactly like the
                                # brace-led oversize below - same error,
                                # written in v1, then close.
                                framing = "v1"
                                sniff_offset = 0
                                del buffer[:]
                                raise ValueError(
                                    "Request exceeded %d bytes without a complete JSON command"
                                    % MAX_REQUEST_BYTES)
                            # The scan above reached the end of the buffer,
                            # except for a partial frame length prefix: its
                            # first byte (0x00/0x01 at sniff_offset) must be
                            # examined again once the rest of it arrives.
                            if not (sniff_offset < len(buffer)
                                    and buffer[sniff_offset] in (0, 1)):
                                sniff_offset = len(buffer)
                            break  # Partial prefix: receive more
                        # The sniff decided from sniff_offset on; move it
                        # onto the first non-whitespace byte so a later
                        # sniff of this same buffer (an incomplete v1
                        # command) starts there instead of rescanning the
                        # leading whitespace again.
                        while (sniff_offset < len(buffer)
                               and buffer[sniff_offset] in (32, 9, 13, 10)):
                            sniff_offset += 1
                        if framing == "v2":
                            decoder.feed(bytes(buffer))
                            del buffer[:]
                            sniff_offset = 0
                            continue
                        # Framing v1: the historical receive path,
                        # unchanged - the whole buffer must be the one
                        # command; a parse failure means "not all here
                        # yet", never a smaller message boundary.
                        if len(buffer) > MAX_REQUEST_BYTES:
                            del buffer[:]
                            raise ValueError(
                                "Request exceeded %d bytes without a complete JSON command"
                                % MAX_REQUEST_BYTES)
                        try:
                            # Only a fully received command decodes and
                            # parses. Either error means "not all here
                            # yet", so wait.
                            command = json.loads(
                                bytes(buffer).decode('utf-8'))
                        except (ValueError, UnicodeDecodeError):
                            break
                        del buffer[:]  # Consume the parsed command
                        sniff_offset = 0
                        dispatch(command)

                except Exception as e:
                    self.log_message("Error handling client data: " + str(e))
                    self.log_message(traceback.format_exc())

                    # Every error that reaches here leaves the request/response
                    # stream in an unknown state, so report it and close rather
                    # than carry on out of step with the client. The report is
                    # written in the framing of the message being answered -
                    # when one was established; unsniffed garbage gets no
                    # reply only before the first message, because after
                    # an exchange the retained framing can express one.
                    if framing is not None:
                        error_response = {
                            "status": "error",
                            "message": str(e)
                        }
                        try:
                            send_response(error_response)
                        except Exception:
                            pass
                    break
        except Exception as e:
            self.log_message("Error in client handler: " + str(e))
        finally:
            try:
                client.close()
            except:
                pass
            self.log_message("Client handler stopped")

    @staticmethod
    def _sniff_framing(buffer, start=0):
        """Decide the framing of the next message from its first bytes.

        Returns "v1", "v2", or None when more bytes are needed; raises
        ValueError when the bytes can start neither protocol. v1 is
        recognised by its first non-whitespace byte being '{' (v1's
        json.loads tolerates leading whitespace, so a padded v1
        request still detects as v1); v2 by a 4-byte big-endian length
        prefix. A legal length (<= 16 MiB) makes the prefix start
        with a 0x00 byte - except exactly 16 MiB, whose prefix is
        01 00 00 00 - so the candidate first bytes are 0x00 and 0x01,
        and '{' can never start a length prefix: the two are
        unambiguous from the first byte on. The decoded length
        itself is the decoder's to validate: handing an out-of-range
        prefix to it produces the precise framed error ("Invalid frame
        length ...") instead of a silent close here. *start* is
        the offset a previous scan of the same buffer reached;
        scanning resumes there.
        """
        # Byte values are space, tab, CR, LF: exactly the whitespace
        # v1's json parsing tolerates as leading padding. Comparing
        # the byte value avoids allocating a slice per byte.
        index = start
        while index < len(buffer) and buffer[index] in (32, 9, 13, 10):
            index += 1
        if index == len(buffer):
            return None  # Only whitespace so far
        if buffer[index:index + 1] == b"{":
            return "v1"
        if index > 0:
            raise ValueError(
                "Unrecognised framing: a frame length prefix may not "
                "follow whitespace")
        if buffer[0:1] not in (b"\x00", b"\x01"):
            raise ValueError(
                "Unrecognised framing: first byte is neither '{' nor "
                "the start of a frame length prefix")
        if len(buffer) < 4:
            return None  # Length prefix not fully here yet
        return "v2"

    def _batch_inline_active(self):
        """True while an execute_batch task is dispatching on this thread.

        The batch task sets this thread-locally for its duration; command
        dispatch that observes it runs inline instead of scheduling,
        because the batch already holds the one main-thread scheduling
        (and a task queued from the main thread could not run until the
        batch returns anyway).
        """
        state = getattr(self, "_dispatch_state", None)
        return state is not None and getattr(state, "on_main_thread", False)

    def _schedule_main_thread_task(self, task, timing_state):
        """Schedule *task* on Live's main thread, optionally timed.

        With timing_state None this is exactly the historical behaviour
        (including running the task inline when already on the main
        thread). With timing requested, the task is wrapped so queue_ms /
        execution_ms can be reported; the finish stamp is taken just
        after the task returns, i.e. just after it queued its response.

        While an execute_batch task is dispatching on this thread, the
        task runs inline instead: the batch's single scheduling is the
        hop for every command it contains.
        """
        if timing_state is not None:
            def timed_task():
                timing_state["main_started"] = time.perf_counter()
                try:
                    task()
                finally:
                    timing_state["main_finished"] = time.perf_counter()

            runnable = timed_task
        else:
            runnable = task

        if self._batch_inline_active():
            runnable()
            return

        try:
            self.schedule_message(0, runnable)
        except AssertionError:
            # If we're already on the main thread, execute directly
            runnable()

    def _process_command(self, command):
        """Process a command from the client and return a response"""
        # SI-249: a non-dict command cannot be dispatched; return the
        # entry error envelope instead of escaping with AttributeError.
        if not isinstance(command, dict):
            return {"status": "error", "message": "Invalid command: must be an object"}
        command_type = command.get("type", "")
        params = command.get("params", {})
        if "type" in command and not isinstance(command_type, str):
            return {"status": "error", "message": "Invalid command type: must be a string"}
        if "params" in command and not isinstance(params, dict):
            return {"status": "error", "message": "Invalid params: must be an object"}

        # MCP-01 native timing (opt-in per request). A command carrying a
        # "request_id" -- or "timing": true -- gets an agent0_timing block
        # on its response envelope and its request_id echoed. Requests that
        # do not opt in take the historical path untouched: no clock reads
        # beyond this membership check and no extra response keys.
        timing_state = None
        # SI-245: the timing opt-in is a real bool or absent. A
        # truthiness read opted a caller sending "false" INTO
        # timing; refuse any other type by name, in the style of
        # the request-id guard below (this head runs before the
        # dispatch try, so the envelope is returned directly).
        if "timing" in command:
            try:
                _require_bool(command.get("timing"), "timing")
            except ValueError as e:
                return {"status": "error", "message": str(e)}
        if "request_id" in command or command.get("timing"):
            if "request_id" in command and not _is_valid_request_id(command.get("request_id")):
                return {"status": "error", "message": "Invalid request ID"}
            timing_state = {
                "started": time.perf_counter(),
                "request_id": command.get("request_id"),
            }
        
        # Initialize response
        response = {
            "status": "success",
            "result": {}
        }
        
        try:
            # Route the command to the appropriate handler
            if command_type == "get_script_info":
                response["result"] = self._get_script_info()
            elif command_type == "execute_batch":
                # MCP-02: run the whole command list inside one
                # main-thread scheduling (see _execute_batch).
                batch_response = self._execute_batch(params, timing_state)
                if batch_response.get("status") == "error":
                    response["status"] = "error"
                    response["message"] = batch_response.get(
                        "message", "Batch failed")
                else:
                    response["result"] = batch_response.get("result", {})
            elif command_type == "get_session_info":
                response["result"] = self._get_session_info()
            elif command_type == "get_track_info":
                track_index = params.get("track_index", 0)
                # MCP-05: projection flags are forwarded only when the
                # caller sent them, so an unprojected call is exactly
                # the pre-projection one.
                info_kwargs = {}
                for key in ("include_clip_slots", "include_devices"):
                    if key in params:
                        info_kwargs[key] = params[key]
                response["result"] = self._get_track_info(
                    track_index, **info_kwargs)
            # "main"-class commands (COMMAND_TABLE) share this
            # scheduled branch; the bespoke "main" branches
            # (set_device_parameter, set_passive_listeners) sit below.
            elif command_type in _SCHEDULED_GROUP_COMMANDS:
                # Use a thread-safe approach with a response queue
                response_queue = queue.Queue()
                
                # Define a function to execute on the main thread
                def main_thread_task():
                    try:
                        result = None
                        if command_type == "create_midi_track":
                            index = params.get("index", -1)
                            result = self._create_midi_track(index)
                        elif command_type == "create_audio_track":
                            index = params.get("index", -1)
                            result = self._create_audio_track(index)
                        elif command_type == "set_track_name":
                            track_index = params.get("track_index", 0)
                            name = params.get("name", "")
                            result = self._set_track_name(track_index, name)
                        elif command_type == "create_clip":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            length = params.get("length", 4.0)
                            result = self._create_clip(track_index, clip_index, length)
                        elif command_type == "create_audio_clip":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            path = params.get("path", "")
                            result = self._create_audio_clip(track_index, clip_index, path)
                        elif command_type == "add_notes_to_clip":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            notes = params.get("notes", [])
                            result = self._add_notes_to_clip(track_index, clip_index, notes)
                        elif command_type == "clear_notes_from_clip":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            result = self._clear_notes_from_clip(track_index, clip_index)
                        elif command_type == "set_clip_name":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            name = params.get("name", "")
                            result = self._set_clip_name(track_index, clip_index, name)
                        elif command_type == "set_arrangement_clip_name":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            name = params.get("name", "")
                            result = self._set_arrangement_clip_name(track_index, clip_index, name)
                        elif command_type == "set_tempo":
                            tempo = params.get("tempo", 120.0)
                            result = self._set_tempo(tempo)
                        elif command_type == "fire_clip":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            result = self._fire_clip(track_index, clip_index)
                        elif command_type == "stop_clip":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            result = self._stop_clip(track_index, clip_index)
                        elif command_type == "delete_clip":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            result = self._delete_clip(track_index, clip_index)
                        elif command_type == "start_playback":
                            result = self._start_playback()
                        elif command_type == "stop_playback":
                            result = self._stop_playback()
                        elif command_type == "load_instrument_or_effect":
                            track_index = params.get("track_index", 0)
                            uri = params.get("uri", "")
                            result = self._load_instrument_or_effect(track_index, uri)
                        elif command_type == "load_browser_item":
                            track_index = params.get("track_index", 0)
                            item_uri = params.get("item_uri", "")
                            result = self._load_browser_item(track_index, item_uri)
                        # ── Arrangement view commands ──────────────────────────────
                        elif command_type == "switch_to_arrangement_view":
                            result = self._switch_to_arrangement_view()
                        elif command_type == "set_current_song_time":
                            time_val = params.get("time", 0.0)
                            result = self._set_current_song_time(time_val)
                        elif command_type == "duplicate_session_clip_to_arrangement":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            destination_time = params.get("destination_time", 0.0)
                            result = self._duplicate_session_clip_to_arrangement(
                                track_index, clip_index, destination_time)
                        elif command_type == "map_rack_magnitude":
                            track_index = params.get("track_index", 0)
                            device_index = params.get("device_index", 0)
                            macro_name = params.get("macro_name", "Magnitude")
                            result = self._map_rack_magnitude(
                                track_index, device_index, macro_name)
                        elif command_type == "inspect_rack":
                            track_index = params.get("track_index", 0)
                            device_index = params.get("device_index", 0)
                            result = self._inspect_rack(track_index, device_index)
                        elif command_type == "create_locator":
                            name = params.get("name", "")
                            time_val = params.get("time", 0.0)
                            result = self._create_locator(name, time_val)

                        # Put the result in the queue
                        response_queue.put({"status": "success", "result": result})
                    except Exception as e:
                        self.log_message("Error in main thread task: " + str(e))
                        self.log_message(traceback.format_exc())
                        response_queue.put({"status": "error", "message": str(e)})
                
                # Schedule the task to run on the main thread
                self._schedule_main_thread_task(main_thread_task, timing_state)
                
                # create_audio_clip decodes/imports the file on the main
                # thread and needs more than the default headroom.
                long_running_commands = {"create_audio_clip": 60.0}
                queue_timeout = long_running_commands.get(command_type, 10.0)
                try:
                    task_response = response_queue.get(timeout=queue_timeout)
                    if task_response.get("status") == "error":
                        response["status"] = "error"
                        response["message"] = task_response.get("message", "Unknown error")
                    else:
                        response["result"] = task_response.get("result", {})
                except queue.Empty:
                    response["status"] = "error"
                    response["message"] = "Timeout waiting for operation to complete"
            elif command_type == "get_browser_item":
                uri = params.get("uri", None)
                path = params.get("path", None)
                response["result"] = self._get_browser_item(uri, path)
            elif command_type == "get_browser_categories":
                category_type = params.get("category_type", "all")
                response["result"] = self._get_browser_categories(category_type)
            elif command_type == "get_browser_items":
                path = params.get("path", "")
                item_type = params.get("item_type", "all")
                response["result"] = self._get_browser_items(path, item_type)
            # Add the new browser commands
            elif command_type == "get_browser_tree":
                category_type = params.get("category_type", "all")
                response["result"] = self.get_browser_tree(category_type)
            elif command_type == "get_browser_items_at_path":
                path = params.get("path", "")
                # MCP-07: paging is opt-in; forward only the values
                # the caller sent, so an unpaged call is exactly the
                # pre-paging one (and a script without the paged form
                # simply never sees them).
                page_kwargs = {}
                for key in ("limit", "offset"):
                    if key in params:
                        page_kwargs[key] = params[key]
                response["result"] = self.get_browser_items_at_path(
                    path, **page_kwargs)
            elif command_type == "search_browser":
                # MCP-07: script-side name/URI search with paging;
                # see _search_browser.
                response["result"] = self._search_browser(params)
            elif command_type == "refresh_browser_cache":
                # MCP-07: explicit invalidation of the browser caches.
                response["result"] = self._refresh_browser_cache()
            # Read-only arrangement command – no main-thread scheduling required
            elif command_type == "get_arrangement_clips":
                track_index = params.get("track_index", 0)
                response["result"] = self._get_arrangement_clips(track_index)
            # Dataset / state-snapshot reads
            elif command_type == "get_clip_notes":
                track_index = params.get("track_index", 0)
                clip_index = params.get("clip_index", 0)
                response["result"] = self._get_clip_notes(track_index, clip_index)
            elif command_type == "get_device_parameters":
                track_index = params.get("track_index", 0)
                device_index = params.get("device_index", 0)
                # MCP-06: subset selection is opt-in; forward only the
                # selectors the caller actually sent, so an unsubsetted
                # call is exactly the pre-subset one (and a script
                # without the subset form simply never sees them).
                subset_kwargs = {}
                for key in ("parameter_indices", "parameter_names"):
                    if key in params:
                        subset_kwargs[key] = params[key]
                response["result"] = self._get_device_parameters(
                    track_index, device_index, **subset_kwargs)
            elif command_type == "get_session_snapshot":
                include_notes = params.get("include_notes", True)
                include_params = params.get("include_params", True)
                # MCP-05: projection params are opt-in; forward only
                # the ones the caller actually sent so an unprojected
                # call is exactly the pre-projection one.
                snapshot_kwargs = {}
                for key in ("track_indices", "include_scenes",
                            "include_return_tracks", "include_master_track",
                            "include_cue_points", "include_clip_slots",
                            "include_devices", "include_arrangement_clips",
                            "include_automation"):
                    if key in params:
                        snapshot_kwargs[key] = params[key]
                response["result"] = self._get_session_snapshot(
                    include_notes=include_notes,
                    include_params=include_params,
                    **snapshot_kwargs
                )
            elif command_type == "drain_passive_events":
                response["result"] = self._drain_passive_events()
            elif command_type == "set_device_parameter":
                response_queue = queue.Queue()

                def main_thread_task():
                    try:
                        result = self._set_device_parameter(
                            params.get("track_index", 0),
                            params.get("device_index", 0),
                            params.get("parameter_index", 0),
                            params.get("value", 0.0),
                        )
                        response_queue.put({"status": "success", "result": result})
                    except Exception as e:
                        self.log_message("Error in main thread task: " + str(e))
                        self.log_message(traceback.format_exc())
                        response_queue.put({"status": "error", "message": str(e)})

                self._schedule_main_thread_task(main_thread_task, timing_state)

                try:
                    task_response = response_queue.get(timeout=10.0)
                    if task_response.get("status") == "error":
                        response["status"] = "error"
                        response["message"] = task_response.get("message", "Unknown error")
                    else:
                        response["result"] = task_response.get("result", {})
                except queue.Empty:
                    response["status"] = "error"
                    response["message"] = "Timeout waiting for operation to complete"
            elif command_type == "set_passive_listeners":
                # MCP-08: listener registration touches the LOM, so
                # the toggle runs on the main thread like the other
                # state-changing commands. An omitted "enabled" just
                # reports the current state.
                response_queue = queue.Queue()

                def main_thread_task():
                    try:
                        if "enabled" in params:
                            # SI-245: pass the caller's value through
                            # untouched - _set_passive_listeners
                            # requires a real bool by name; the old
                            # bool(...) cast here took the TRUE branch
                            # for the string "false" before the
                            # handler ever saw the value.
                            result = self._set_passive_listeners(
                                params.get("enabled"))
                        else:
                            result = {
                                "enabled": bool(getattr(
                                    self, "_passive_listeners_enabled",
                                    False)),
                                "changed": False,
                                "listener_count":
                                    self._passive_listener_count(),
                            }
                        response_queue.put(
                            {"status": "success", "result": result})
                    except Exception as e:
                        self.log_message(
                            "Error in main thread task: " + str(e))
                        self.log_message(traceback.format_exc())
                        response_queue.put(
                            {"status": "error", "message": str(e)})

                self._schedule_main_thread_task(main_thread_task, timing_state)

                try:
                    task_response = response_queue.get(timeout=10.0)
                    if task_response.get("status") == "error":
                        response["status"] = "error"
                        response["message"] = task_response.get(
                            "message", "Unknown error")
                    else:
                        response["result"] = task_response.get("result", {})
                except queue.Empty:
                    response["status"] = "error"
                    response["message"] = \
                        "Timeout waiting for operation to complete"
            elif command_type == "set_device_parameters":
                # MCP-06: apply the whole parameter list inside one
                # main-thread scheduling (see _set_device_parameters).
                bulk_response = self._set_device_parameters(params, timing_state)
                if bulk_response.get("status") == "error":
                    response["status"] = "error"
                    response["message"] = bulk_response.get(
                        "message", "Bulk parameter set failed")
                else:
                    response["result"] = bulk_response.get("result", {})
            else:
                response["status"] = "error"
                response["message"] = "Unknown command: " + command_type
        except Exception as e:
            self.log_message("Error processing command: " + str(e))
            self.log_message(traceback.format_exc())
            response["status"] = "error"
            response["message"] = str(e)
        
        if timing_state is not None:
            _finish_agent0_timing(response, timing_state)
        return response
    
    def _execute_batch(self, params, timing_state=None):
        """Execute an ordered list of commands in one main-thread task.

        The wire command ``execute_batch`` (MCP-02) takes ``commands``: a
        list of ``{"type": ..., "params": {...}}`` items, and an optional
        ``stop_on_error`` (default False). The whole list runs inside ONE
        schedule_message hop: this method's task executes on Live's main
        thread and dispatches each item through _process_command with
        re-scheduling suppressed (see _schedule_main_thread_task), so a
        batch of N commands costs one socket round trip and one queue
        wait instead of N of each.

        This is a sequencing primitive, NOT a transaction. Items run in
        order; each item that reaches dispatch produces exactly the
        response envelope a direct call would have produced, while an
        item rejected before dispatch (see _execute_batch_item) reports
        a batch-style error envelope - the same message, without the
        "result": {} key a dispatched call's envelope carries. A failed
        item does not roll back the items before it. With stop_on_error,
        items after the first failure are not run and are reported as
        {"status": "skipped", ...} - never
        silently dropped. The success result is
        {"results": [...], "summary": {"total": n, "succeeded": n,
        "failed": n, "skipped": n}}.
        """
        if not isinstance(params, dict):
            return {"status": "error",
                    "message": "execute_batch requires an object of params"}
        commands = params.get("commands")
        if not isinstance(commands, list):
            return {"status": "error",
                    "message": "execute_batch requires a 'commands' list"}
        if len(commands) > MAX_BATCH_COMMANDS:
            return {"status": "error",
                    "message": "execute_batch accepts at most %d commands"
                               % MAX_BATCH_COMMANDS}
        # SI-245: stop_on_error is a real bool when sent (omitted
        # keeps the default False). By truthiness the string
        # "false" stopped the batch on the first error. Refused in
        # this method's own early-guard style, above.
        if "stop_on_error" in params:
            try:
                stop_on_error = _require_bool(
                    params.get("stop_on_error"), "stop_on_error")
            except ValueError as e:
                return {"status": "error", "message": str(e)}
        else:
            stop_on_error = False

        response_queue = queue.Queue()

        def batch_task():
            results = []
            succeeded = 0
            failed = 0
            skipped = 0
            try:
                state = getattr(self, "_dispatch_state", None)
                if state is None:
                    state = self._dispatch_state = threading.local()
                state.on_main_thread = True
                try:
                    halted = False
                    for item in commands:
                        if halted:
                            results.append({
                                "status": "skipped",
                                "message": "Skipped: stop_on_error after "
                                           "a failed item",
                            })
                            skipped += 1
                            continue
                        item_response = self._execute_batch_item(
                            item, timing_state is not None)
                        results.append(item_response)
                        if item_response.get("status") == "error":
                            failed += 1
                            if stop_on_error:
                                halted = True
                        else:
                            succeeded += 1
                finally:
                    state.on_main_thread = False
            except Exception as e:
                self.log_message("Error in batch task: " + str(e))
                self.log_message(traceback.format_exc())
                response_queue.put({"status": "error", "message": str(e)})
                return
            response_queue.put({"status": "success", "result": {
                "results": results,
                "summary": {"total": len(commands),
                            "succeeded": succeeded,
                            "failed": failed,
                            "skipped": skipped},
            }})

        self._schedule_main_thread_task(batch_task, timing_state)

        # The batch may take as long as its items could have taken one by
        # one: mirror the per-command queue budgets (60 s for
        # create_audio_clip, 10 s otherwise), floored at one budget.
        queue_timeout = 0.0
        for item in commands:
            if isinstance(item, dict) and item.get("type") == "create_audio_clip":
                queue_timeout += 60.0
            else:
                queue_timeout += 10.0
        try:
            return response_queue.get(timeout=max(queue_timeout, 10.0))
        except queue.Empty:
            return {"status": "error",
                    "message": "Timeout waiting for batch to complete"}

    def _execute_batch_item(self, item, timed):
        """Dispatch one execute_batch item through the direct path.

        ``timed`` propagates the batch's own timing opt-in: items of a
        timed batch carry "timing": true, so each per-item envelope gets
        its own agent0_timing block (dispatch/queue/execution for that
        item); items of an untimed batch produce envelopes identical to
        direct calls. Guarded commands keep their guards: a guarded
        item runs the same handler - and the same checks - as the direct
        command, because dispatch is the direct path. Items rejected
        here, before dispatch - a non-object item, a nested
        execute_batch, a non-object "params" - return the batch-style
        error envelope {"status": "error", "message": ...} without the
        "result": {} key; everything that is dispatched returns
        _process_command's envelope, exactly as a direct call.
        """
        if not isinstance(item, dict):
            return {"status": "error",
                    "message": "Batch items must be objects with 'type' "
                               "and 'params'"}
        item_type = item.get("type", "")
        if item_type == "execute_batch":
            # No recursion: a batch inside a batch could not keep the
            # one-scheduling guarantee, and a flat list sequences the
            # same work without it.
            return {"status": "error",
                    "message": "Nested execute_batch is not allowed"}
        item_params = item.get("params", {})
        if not isinstance(item_params, dict):
            return {"status": "error",
                    "message": "Batch item 'params' must be an object"}
        item_command = {"type": item_type, "params": item_params}
        if timed:
            item_command["timing"] = True
        return self._process_command(item_command)

    # Command implementations

    def _get_script_info(self):
        """Handshake payload for MCP server version / capability checks."""
        return {
            "name": "AbletonMCP",
            "script_version": SCRIPT_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "port": DEFAULT_PORT,
            "capabilities": list(SCRIPT_CAPABILITIES),
            "snapshot_schema": "ableton_mcp_snapshot_v2",
            "passive_listeners": True,
            "agent0_passive_listeners_negotiated": True,
            "passive_listeners_enabled": bool(
                getattr(self, "_passive_listeners_enabled", False)),
            "agent0_request_ids": True,
            "agent0_framing": ["v1", "v2"],
            "agent0_capabilities_complete": True,
            "agent0_host_revision": 1,
        }
    
    def _safe_song_property(self, attr, cast, default):
        """Read self._song.<attr> with cast, returning default on common failures.
        Catches only narrow exceptions so genuine bugs still surface."""
        try:
            return cast(getattr(self._song, attr))
        except (AttributeError, TypeError, ValueError):
            return default

    def _get_session_info(self):
        """Get information about the current session"""
        try:
            result = {
                "tempo": self._song.tempo,
                "signature_numerator": self._song.signature_numerator,
                "signature_denominator": self._song.signature_denominator,
                "track_count": len(self._song.tracks),
                "return_track_count": len(self._song.return_tracks),
                "master_track": {
                    "name": "Master",
                    "volume": self._song.master_track.mixer_device.volume.value,
                    "panning": self._song.master_track.mixer_device.panning.value
                },
                # Read via _safe_song_property so an attribute missing on a
                # given Live version falls back to its default.
                "is_playing":        self._safe_song_property("is_playing",        bool,  False),
                "current_song_time": self._safe_song_property("current_song_time", float, 0.0),
                "song_length":       self._safe_song_property("song_length",       float, 0.0),
                "loop":              self._safe_song_property("loop",              bool,  False),
                "loop_start":        self._safe_song_property("loop_start",        float, 0.0),
                "loop_length":       self._safe_song_property("loop_length",       float, 0.0),
            }
            return result
        except Exception as e:
            self.log_message("Error getting session info: " + str(e))
            raise
    
    def _get_track_info(self, track_index, include_clip_slots=True,
                        include_devices=True):
        """Get information about a track.

        MCP-05 projection: include_clip_slots / include_devices may be
        False to omit that section entirely -- it is then not serialised
        at all. Both default True: the pre-projection result.
        """
        try:
            track_index = _require_index(
                track_index, "track_index", len(self._song.tracks),
                "Track index out of range")
            # SI-245: projection flags are real bools. Read by
            # truthiness, the string "false" included the section
            # it was sent to omit.
            include_clip_slots = _require_bool(
                include_clip_slots, "include_clip_slots")
            include_devices = _require_bool(
                include_devices, "include_devices")

            track = self._song.tracks[track_index]

            result = {
                "index": track_index,
                "name": track.name,
                "is_audio_track": track.has_audio_input,
                "is_midi_track": track.has_midi_input,
                "mute": track.mute,
                "solo": track.solo,
                "arm": self._safe_arm(track),
                "volume": track.mixer_device.volume.value,
                "panning": track.mixer_device.panning.value,
            }

            if include_clip_slots:
                # Get clip slots
                clip_slots = []
                for slot_index, slot in enumerate(track.clip_slots):
                    clip_info = None
                    if slot.has_clip:
                        clip = slot.clip
                        clip_info = {
                            "name": clip.name,
                            "length": clip.length,
                            "is_playing": clip.is_playing,
                            "is_recording": clip.is_recording
                        }

                    clip_slots.append({
                        "index": slot_index,
                        "has_clip": slot.has_clip,
                        "clip": clip_info
                    })
                result["clip_slots"] = clip_slots

            if include_devices:
                # Get devices
                devices = []
                for device_index, device in enumerate(track.devices):
                    devices.append({
                        "index": device_index,
                        "name": device.name,
                        "class_name": device.class_name,
                        "type": self._get_device_type(device)
                    })
                result["devices"] = devices

            return result
        except Exception as e:
            self.log_message("Error getting track info: " + str(e))
            raise
    def _safe_arm(self, track):
        """Read track.arm, returning False for tracks that have no arm state.

        Live raises RuntimeError("Master and Return Tracks have no 'Arm'
        state!") for group tracks as well as return and main tracks. A
        `getattr(track, "arm", False)` does not guard this: the attribute
        exists, so getattr's default never applies -- reading it is what
        throws, and the error is a RuntimeError rather than an AttributeError.

        Check can_be_armed first so the common path does not rely on raising,
        and keep a narrow catch for Live versions that do not expose that
        property on every track type.
        """
        try:
            if not getattr(track, "can_be_armed", False):
                return False
            return bool(track.arm)
        except (AttributeError, RuntimeError):
            return False

    def _create_midi_track(self, index):
        """Create a new MIDI track at the specified index"""
        try:
            # SI-242: validate the index type before the LOM call
            # (and the track-list indexing below) can consume a
            # wrong-type value; the position itself is unbounded
            # here (-1 means "at the end"), so no range check.
            index = _require_index(index, "index", None)
            # Create the track
            self._song.create_midi_track(index)
            
            # Get the new track
            new_track_index = len(self._song.tracks) - 1 if index == -1 else index
            new_track = self._song.tracks[new_track_index]
            
            result = {
                "index": new_track_index,
                "name": new_track.name
            }
            return result
        except Exception as e:
            self.log_message("Error creating MIDI track: " + str(e))
            raise

    def _create_audio_track(self, index):
        """Create a new audio track at the specified index"""
        try:
            # SI-242: as in _create_midi_track - type only, no range.
            index = _require_index(index, "index", None)
            # Create the track
            self._song.create_audio_track(index)

            # Get the new track
            new_track_index = len(self._song.tracks) - 1 if index == -1 else index
            new_track = self._song.tracks[new_track_index]

            result = {
                "index": new_track_index,
                "name": new_track.name
            }
            return result
        except Exception as e:
            self.log_message("Error creating audio track: " + str(e))
            raise


    def _set_track_name(self, track_index, name):
        """Set the name of a track"""
        try:
            track_index = _require_index(
                track_index, "track_index", len(self._song.tracks),
                "Track index out of range")
            
            # Set the name
            track = self._song.tracks[track_index]
            name = _require_string(name, "name")
            track.name = name
            
            result = {
                "name": track.name
            }
            return result
        except Exception as e:
            self.log_message("Error setting track name: " + str(e))
            raise
    
    def _create_clip(self, track_index, clip_index, length):
        """Create a new MIDI clip in the specified track and clip slot"""
        try:
            track_index = _require_index(
                track_index, "track_index", len(self._song.tracks),
                "Track index out of range")
            
            track = self._song.tracks[track_index]
            
            clip_index = _require_index(
                clip_index, "clip_index", len(track.clip_slots),
                "Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            # Check if the clip slot already has a clip
            if clip_slot.has_clip:
                raise Exception("Clip slot already has a clip")
            
            # Create the clip
            length = _require_finite_number(length, "length")
            clip_slot.create_clip(length)
            
            result = {
                "name": clip_slot.clip.name,
                "length": clip_slot.clip.length
            }
            return result
        except Exception as e:
            self.log_message("Error creating clip: " + str(e))
            raise

    def _create_audio_clip(self, track_index, clip_index, path):
        """Create an audio clip in the specified audio track clip slot by importing a file.

        Requires Ableton Live 12.0.5 or newer (the underlying
        ClipSlot.create_audio_clip Live API was introduced in 12.0.5 — it is
        not available in earlier 12.0.x releases).
        """
        try:
            if not path:
                raise ValueError("Audio file path is required")

            _require_string(path, "path")

            if not os.path.isabs(path):
                raise ValueError("Audio file path must be absolute (got: %s)" % path)

            track_index = _require_index(
                track_index, "track_index", len(self._song.tracks),
                "Track index out of range")

            track = self._song.tracks[track_index]

            if getattr(track, "has_midi_input", False) or not getattr(track, "has_audio_input", True):
                raise ValueError("Track %d is not an audio track" % track_index)

            clip_index = _require_index(
                clip_index, "clip_index", len(track.clip_slots),
                "Clip index out of range")

            clip_slot = track.clip_slots[clip_index]

            if clip_slot.has_clip:
                raise Exception("Clip slot already has a clip")

            if not hasattr(clip_slot, "create_audio_clip"):
                raise Exception(
                    "ClipSlot.create_audio_clip is unavailable in this Ableton Live "
                    "version. Requires Live 12.0.5 or newer."
                )

            clip_slot.create_audio_clip(path)

            result = {
                "name": clip_slot.clip.name,
                "length": clip_slot.clip.length,
                "is_audio_clip": clip_slot.clip.is_audio_clip
            }
            return result
        except Exception as e:
            self.log_message("Error creating audio clip: " + str(e))
            raise

    def _add_notes_to_clip(self, track_index, clip_index, notes):
        """Add MIDI notes to a clip"""
        try:
            track_index = _require_index(
                track_index, "track_index", len(self._song.tracks),
                "Track index out of range")
            
            track = self._song.tracks[track_index]
            
            clip_index = _require_index(
                clip_index, "clip_index", len(track.clip_slots),
                "Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            if not clip_slot.has_clip:
                raise Exception("No clip in slot")
            
            clip = clip_slot.clip
            
            # Convert note data to Live's format. SI-244 (SI-239
            # F5): the container, the entries and every provided
            # field are validated by name before anything reaches
            # Clip.set_notes - a non-list notes, a non-dict entry
            # and bad field values (pitch "A", a NaN pitch, an
            # infinite start_time, a list velocity) all used to
            # fail raw or report success with the bad tuple
            # delivered. Validation runs here, after the clip is
            # fetched, so the no-clip refusal keeps its precedence,
            # and the whole list is converted before set_notes is
            # called, so a bad entry anywhere means set_notes is
            # never called at all.
            notes = _require_list_selector(notes, "notes")
            live_notes = []
            for note in notes:
                if not isinstance(note, dict):
                    raise ValueError(
                        "Invalid notes: entries must be objects")
                pitch = note.get("pitch", 60)
                start_time = note.get("start_time", 0.0)
                duration = note.get("duration", 0.25)
                velocity = note.get("velocity", 100)
                mute = note.get("mute", False)
                # _require_finite_number validates only here: its
                # int-to-float return is deliberately NOT used,
                # because this handler has always forwarded the
                # caller's values verbatim (an int pitch reached
                # set_notes as an int) - using the return would
                # change every well-formed tuple's types. Bools
                # are excluded from numbers by that helper, as
                # the SI-242/SI-243 rules require.
                _require_finite_number(pitch, "pitch")
                _require_finite_number(start_time, "start_time")
                _require_finite_number(duration, "duration")
                _require_finite_number(velocity, "velocity")
                mute = _require_bool(mute, "mute")
                
                live_notes.append((pitch, start_time, duration, velocity, mute))
            
            # Add the notes
            clip.set_notes(tuple(live_notes))
            
            result = {
                "note_count": len(notes)
            }
            return result
        except Exception as e:
            self.log_message("Error adding notes to clip: " + str(e))
            raise
    
    def _set_clip_name(self, track_index, clip_index, name):
        """Set the name of a clip"""
        try:
            track_index = _require_index(
                track_index, "track_index", len(self._song.tracks),
                "Track index out of range")
            
            track = self._song.tracks[track_index]
            
            clip_index = _require_index(
                clip_index, "clip_index", len(track.clip_slots),
                "Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            if not clip_slot.has_clip:
                raise Exception("No clip in slot")
            
            clip = clip_slot.clip
            name = _require_string(name, "name")
            clip.name = name
            
            result = {
                "name": clip.name
            }
            return result
        except Exception as e:
            self.log_message("Error setting clip name: " + str(e))
            raise

    def _set_arrangement_clip_name(self, track_index, clip_index, name):
        """Set the name of a clip placed in the Arrangement timeline.

        clip_index indexes into track.arrangement_clips, in the same order
        as returned by _get_arrangement_clips (i.e. ordered by start_time).
        """
        try:
            track_index = _require_index(
                track_index, "track_index", len(self._song.tracks),
                "Track index out of range")

            track = self._song.tracks[track_index]
            arrangement_clips = list(track.arrangement_clips)

            clip_index = _require_index(
                clip_index, "clip_index", len(arrangement_clips),
                "Clip index out of range")

            clip = arrangement_clips[clip_index]
            name = _require_string(name, "name")
            clip.name = name

            result = {
                "name": clip.name
            }
            return result
        except Exception as e:
            self.log_message("Error setting arrangement clip name: " + str(e))
            raise

    def _set_tempo(self, tempo):
        """Set the tempo of the session"""
        try:
            tempo = _require_finite_number(tempo, "tempo")
            self._song.tempo = tempo
            
            result = {
                "tempo": self._song.tempo
            }
            return result
        except Exception as e:
            self.log_message("Error setting tempo: " + str(e))
            raise
    
    def _fire_clip(self, track_index, clip_index):
        """Fire a clip"""
        try:
            track_index = _require_index(
                track_index, "track_index", len(self._song.tracks),
                "Track index out of range")
            
            track = self._song.tracks[track_index]
            
            clip_index = _require_index(
                clip_index, "clip_index", len(track.clip_slots),
                "Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            if not clip_slot.has_clip:
                raise Exception("No clip in slot")
            
            clip_slot.fire()
            
            result = {
                "fired": True
            }
            return result
        except Exception as e:
            self.log_message("Error firing clip: " + str(e))
            raise
    
    def _stop_clip(self, track_index, clip_index):
        """Stop a clip"""
        try:
            track_index = _require_index(
                track_index, "track_index", len(self._song.tracks),
                "Track index out of range")
            
            track = self._song.tracks[track_index]
            
            clip_index = _require_index(
                clip_index, "clip_index", len(track.clip_slots),
                "Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            clip_slot.stop()
            
            result = {
                "stopped": True
            }
            return result
        except Exception as e:
            self.log_message("Error stopping clip: " + str(e))
            raise

    def _delete_clip(self, track_index, clip_index):
        """Delete the clip in the given clip slot, freeing the slot for reuse."""
        try:
            track_index = _require_index(
                track_index, "track_index", len(self._song.tracks),
                "Track index out of range")

            track = self._song.tracks[track_index]

            clip_index = _require_index(
                clip_index, "clip_index", len(track.clip_slots),
                "Clip index out of range")

            clip_slot = track.clip_slots[clip_index]

            if not clip_slot.has_clip:
                return {"deleted": False, "reason": "Clip slot was already empty"}

            clip_slot.delete_clip()

            return {"deleted": True}
        except Exception as e:
            self.log_message("Error deleting clip: " + str(e))
            raise


    def _start_playback(self):
        """Start playing the session"""
        try:
            self._song.start_playing()
            
            result = {
                "playing": self._song.is_playing
            }
            return result
        except Exception as e:
            self.log_message("Error starting playback: " + str(e))
            raise
    
    def _stop_playback(self):
        """Stop playing the session"""
        try:
            self._song.stop_playing()
            
            result = {
                "playing": self._song.is_playing
            }
            return result
        except Exception as e:
            self.log_message("Error stopping playback: " + str(e))
            raise
    
    # ── Arrangement view implementations ──────────────────────────────────────

    def _switch_to_arrangement_view(self):
        """Switch Ableton's main window to the Arrangement view"""
        try:
            self.application().view.show_view("Arranger")
            return {"view": "Arranger"}
        except Exception as e:
            self.log_message("Error switching to arrangement view: " + str(e))
            raise

    def _set_current_song_time(self, time_val):
        """Move the arrangement playhead to a position in beats"""
        try:
            self._song.current_song_time = _require_finite_number(
                time_val, "time")
            return {"current_song_time": self._song.current_song_time}
        except Exception as e:
            self.log_message("Error setting current song time: " + str(e))
            raise

    def _get_arrangement_clips(self, track_index):
        """Return all clips placed in the Arrangement timeline for a track.

        Each clip dict contains:
          name, start_time, end_time, length, color,
          is_midi_clip, is_audio_clip, is_playing
        """
        try:
            track_index = _require_index(
                track_index, "track_index", len(self._song.tracks),
                "Track index out of range")

            track = self._song.tracks[track_index]
            clips = []

            # track.arrangement_clips is available in Live 11 / 12
            for clip in track.arrangement_clips:
                clips.append({
                    "name": clip.name,
                    "start_time": clip.start_time,
                    "end_time": clip.end_time,
                    "length": clip.length,
                    "color": clip.color,
                    "is_midi_clip": clip.is_midi_clip,
                    "is_audio_clip": clip.is_audio_clip,
                    "is_playing": clip.is_playing
                })

            return {
                "track_index": track_index,
                "track_name": track.name,
                "clip_count": len(clips),
                "clips": clips
            }
        except Exception as e:
            self.log_message("Error getting arrangement clips: " + str(e))
            raise

    def _clear_notes_from_clip(self, track_index, clip_index):
        """Remove all MIDI notes from a Session clip.

        Pairs with _add_notes_to_clip to make a real replace (clear, then add),
        which the write-only API otherwise can't do. Counts notes first so the
        result can report how many were removed.
        """
        try:
            track_index = _require_index(
                track_index, "track_index", len(self._song.tracks),
                "Track index out of range")

            track = self._song.tracks[track_index]

            clip_index = _require_index(
                clip_index, "clip_index", len(track.clip_slots),
                "Clip index out of range")

            clip_slot = track.clip_slots[clip_index]

            if not clip_slot.has_clip:
                raise Exception("No clip in slot")

            clip = clip_slot.clip

            if not clip.is_midi_clip:
                raise Exception("Clip is not a MIDI clip; no notes to clear")

            length = clip.length

            # Count existing notes for the report (best-effort; never fatal).
            cleared = 0
            try:
                getter = getattr(clip, "get_notes_extended", None)
                if getter is not None:
                    cleared = len(list(getter(0, 128, 0.0, length)))
                else:
                    cleared = len(list(clip.get_notes(0.0, 0, length, 128)))
            except Exception:
                cleared = 0

            # Remove every note across the full pitch/time range. Prefer the
            # modern API (Live 11+); fall back to the legacy signature. Argument
            # order mirrors the get/remove _extended family:
            #   remove_notes_extended(from_pitch, pitch_span, from_time, time_span)
            # vs the legacy remove_notes(from_time, from_pitch, time_span, pitch_span).
            remover = getattr(clip, "remove_notes_extended", None)
            if remover is not None:
                remover(0, 128, 0.0, length)
            else:
                clip.remove_notes(0.0, 0, length, 128)

            return {
                "track_index": track_index,
                "clip_index": clip_index,
                "clip_name": clip.name,
                "cleared_count": cleared,
            }
        except Exception as e:
            self.log_message("Error clearing notes from clip: " + str(e))
            raise

    def _duplicate_session_clip_to_arrangement(self, track_index, clip_index, destination_time):
        """Copy a Session-view clip into the Arrangement timeline.

        Uses the real Live API:
          track.duplicate_clip_to_arrangement(clip, destination_time)

        Available in Live 11 / 12.  destination_time is in beats from the
        start of the arrangement.
        """
        try:
            track_index = _require_index(
                track_index, "track_index", len(self._song.tracks),
                "Track index out of range")

            track = self._song.tracks[track_index]

            clip_index = _require_index(
                clip_index, "clip_index", len(track.clip_slots),
                "Clip slot index out of range")

            clip_slot = track.clip_slots[clip_index]

            if not clip_slot.has_clip:
                raise Exception(
                    "No clip in slot " + str(clip_index) +
                    " on track " + str(track_index)
                )

            clip = clip_slot.clip

            # Duplicate to arrangement at the requested beat position
            track.duplicate_clip_to_arrangement(
                clip, _require_finite_number(
                    destination_time, "destination_time"))

            return {
                "success": True,
                "track_index": track_index,
                "track_name": track.name,
                "clip_name": clip.name,
                "destination_time": destination_time
            }
        except Exception as e:
            self.log_message("Error duplicating clip to arrangement: " + str(e))
            raise

    def _create_locator(self, name, time_val):
        """Create (or rename) a named locator at the given beat position.

        Uses Live's Song.set_or_delete_cue(), which toggles a cue at the
        current_song_time. We temporarily move the playhead, toggle, then
        restore. If a cue already exists at that time we just rename it
        instead of toggling (which would delete it).
        """
        try:
            song = self._song
            name = _require_string(name, "name")
            target_time = _require_finite_number(time_val, "time")
            tolerance = 1e-3

            # See if a cue already exists at (or near) the target time
            existing = None
            for cue in song.cue_points:
                if abs(cue.time - target_time) < tolerance:
                    existing = cue
                    break

            original_time = song.current_song_time

            if existing is None:
                # Move playhead, toggle to create, then locate the new cue
                song.current_song_time = target_time
                song.set_or_delete_cue()
                for cue in song.cue_points:
                    if abs(cue.time - target_time) < tolerance:
                        existing = cue
                        break
                # Restore playhead
                try:
                    song.current_song_time = original_time
                except Exception:
                    pass

            if existing is None:
                raise Exception("Failed to create cue at time " + str(target_time))

            if name:
                try:
                    existing.name = name
                except Exception as e:
                    self.log_message("Could not rename locator: " + str(e))

            return {
                "success": True,
                "time": existing.time,
                "name": existing.name,
            }
        except Exception as e:
            self.log_message("Error creating locator: " + str(e))
            raise

    # ── Browser implementations ───────────────────────────────────────────────

    def _get_browser_item(self, uri, path):
        """Get a browser item by URI or path"""
        try:
            # Access the application's browser instance instead of creating a new one
            app = self.application()
            if not app:
                raise RuntimeError("Could not access Live application")
                
            result = {
                "uri": uri,
                "path": path,
                "found": False
            }
            
            # Try to find by URI first if provided
            if uri:
                item = self._find_browser_item_by_uri(app.browser, uri)
                if item:
                    result["found"] = True
                    result["item"] = {
                        "name": item.name,
                        "is_folder": item.is_folder,
                        "is_device": item.is_device,
                        "is_loadable": item.is_loadable,
                        "uri": item.uri
                    }
                    return result
            
            # If URI not provided or not found, try by path
            if path:
                # SI-246: refuse a non-string path by name before it
                # can die at the split below with a raw interpreter
                # message (SI-239 F7). The URI side of this handler
                # was refused by name in SI-237; the path side was
                # outside that sweep and no cache key is involved.
                # _require_string is the SI-243 helper, the same one
                # _create_audio_clip uses for its path. The guard
                # sits inside the truthiness branch, so the omitted
                # path (the dispatchers default of None) and every
                # other falsy value keep their exact prior behaviour
                # - they never reached the split - and a URI hit
                # above still returns before path navigation.
                path = _require_string(path, "path")
                # Parse the path and navigate to the specified item
                path_parts = path.split("/")
                
                # Determine the root based on the first part
                current_item = None
                if path_parts[0].lower() == "instruments":
                    current_item = app.browser.instruments
                elif path_parts[0].lower() == "sounds":
                    current_item = app.browser.sounds
                elif path_parts[0].lower() == "drums":
                    current_item = app.browser.drums
                elif path_parts[0].lower() == "audio_effects":
                    current_item = app.browser.audio_effects
                elif path_parts[0].lower() == "midi_effects":
                    current_item = app.browser.midi_effects
                else:
                    # Default to instruments if not specified
                    current_item = app.browser.instruments
                    # Don't skip the first part in this case
                    path_parts = ["instruments"] + path_parts
                
                # Navigate through the path
                for i in range(1, len(path_parts)):
                    part = path_parts[i]
                    if not part:  # Skip empty parts
                        continue
                    
                    found = False
                    for child in current_item.children:
                        if child.name.lower() == part.lower():
                            current_item = child
                            found = True
                            break
                    
                    if not found:
                        result["error"] = "Path part '{0}' not found".format(part)
                        return result
                
                # Found the item
                result["found"] = True
                result["item"] = {
                    "name": current_item.name,
                    "is_folder": current_item.is_folder,
                    "is_device": current_item.is_device,
                    "is_loadable": current_item.is_loadable,
                    "uri": current_item.uri
                }
            
            return result
        except Exception as e:
            self.log_message("Error getting browser item: " + str(e))
            self.log_message(traceback.format_exc())
            raise   
    
    
    
    def _load_instrument_or_effect(self, track_index, uri):
        """Load an instrument or effect onto a track by its browser URI.

        The command dispatcher above calls this method, but it was never
        defined — and "load_instrument_or_effect" was missing from the list of
        main-thread commands as well, so the command fell through to the final
        "Unknown command" branch. Loading a device is exactly what
        _load_browser_item does, so delegate to it; the only difference is the
        parameter name the MCP server uses ("uri" vs "item_uri").
        """
        return self._load_browser_item(track_index, uri)

    def _load_browser_item(self, track_index, item_uri):
        """Load a browser item onto a track by its URI"""
        try:
            track_index = _require_index(
                track_index, "track_index", len(self._song.tracks),
                "Track index out of range")
            
            track = self._song.tracks[track_index]
            
            # Access the application's browser instance instead of creating a new one
            app = self.application()
            
            # Find the browser item by URI
            item = self._find_browser_item_by_uri(app.browser, item_uri)
            
            if not item:
                raise ValueError("Browser item with URI '{0}' not found".format(item_uri))
            
            # Select the track
            self._song.view.selected_track = track
            
            # Load the item
            app.browser.load_item(item)

            # MCP-07: loading is the one browser-directed mutation
            # the script performs, and the browser caches are only
            # sound while the library is unchanged from the script's
            # point of view - drop them so the next read or lookup
            # re-walks (the audit pins invalidation on load).
            self._invalidate_browser_caches()
            
            result = {
                "loaded": True,
                "item_name": item.name,
                "track_name": track.name,
                "uri": item_uri
            }
            return result
        except Exception as e:
            self.log_message("Error loading browser item: {0}".format(str(e)))
            self.log_message(traceback.format_exc())
            raise
    
    # Substring markers that point a URI at a likely root. Unmatched URIs fall
    # back to the default search order.
    _URI_ROOT_HINTS = (
        ('plugins',       ('vst:', 'vst3:', 'au:', 'query:plugins', 'plugin#')),
        ('max_for_live',  ('max for live', 'maxforlive', 'm4l', 'query:max')),
        ('user_library',  ('user library', 'userlibrary', 'query:user library', 'query:user-library')),
        ('packs',         ('query:packs', '/packs/')),
        ('samples',       ('query:samples', 'sample:', '/samples/')),
        ('drums',         ('query:drums', '/drums/')),
        ('instruments',   ('query:instruments', '/instruments/')),
        ('sounds',        ('query:sounds', '/sounds/')),
        ('audio_effects', ('query:audio effects', 'audioeffects', '/audio_effects/')),
        ('midi_effects',  ('query:midi effects', 'midieffects', '/midi_effects/')),
    )

    def _order_roots_by_uri(self, roots, uri):
        """Reorder ``roots`` so the URI's likely root is walked first."""
        if not isinstance(uri, (bytes, str)) or not uri:
            return roots
        lowered = uri.lower()
        for attr, markers in self._URI_ROOT_HINTS:
            if any(m in lowered for m in markers):
                head = [(a, r) for (a, r) in roots if a == attr]
                tail = [(a, r) for (a, r) in roots if a != attr]
                return head + tail
        return roots

    def _find_browser_item_by_uri(self, browser_or_item, uri, max_depth=10, current_depth=0):
        """Find a browser item by its URI.

        Top-level lookups are memoised on ``self._uri_cache`` so repeated
        loads of the same URI don't re-walk the entire browser tree.
        MCP-07: the memo is an LRU bounded at
        MAX_BROWSER_URI_CACHE_ENTRIES and is invalidated with the rest
        of the browser caches (see _invalidate_browser_caches); it used
        to be a plain dict that grew for the script's lifetime and
        could keep pointing at an item the library no longer has.
        """
        if current_depth == 0:
            # SI-237: refuse an unhashable uri by name before it
            # can crash the memo lookup as a raw TypeError.
            _require_hashable_cache_key(uri, "uri")
            uri_cache, _ = self._browser_cache_state()
            cached = self._browser_cache_get(uri_cache, uri)
            if cached is not None:
                return cached
            result = self._walk_browser_for_uri(browser_or_item, uri, max_depth, 0)
            if result is not None:
                self._browser_cache_put(uri_cache, uri, result,
                                        MAX_BROWSER_URI_CACHE_ENTRIES)
            return result
        return self._walk_browser_for_uri(browser_or_item, uri, max_depth, current_depth)

    def _walk_browser_for_uri(self, browser_or_item, uri, max_depth, current_depth):
        """Recursive walk used by :py:meth:`_find_browser_item_by_uri`."""
        try:
            # Check if this is the item we're looking for
            if hasattr(browser_or_item, 'uri') and browser_or_item.uri == uri:
                return browser_or_item

            # Stop recursion if we've reached max depth
            if current_depth >= max_depth:
                return None

            # Check if this is a browser with root categories
            if hasattr(browser_or_item, 'instruments'):
                roots = [
                    ('instruments', browser_or_item.instruments),
                    ('sounds', browser_or_item.sounds),
                    ('drums', browser_or_item.drums),
                    ('audio_effects', browser_or_item.audio_effects),
                    ('midi_effects', browser_or_item.midi_effects),
                ]
                for extra_attr in ('plugins', 'max_for_live', 'user_library', 'packs', 'samples'):
                    if hasattr(browser_or_item, extra_attr):
                        try:
                            roots.append((extra_attr, getattr(browser_or_item, extra_attr)))
                        except (AttributeError, RuntimeError) as e:
                            self.log_message("Could not access browser.{0}: {1}".format(extra_attr, str(e)))

                for _attr, category in self._order_roots_by_uri(roots, uri):
                    item = self._find_browser_item_by_uri(category, uri, max_depth, current_depth + 1)
                    if item:
                        return item

                return None

            # Check if this item has children
            if hasattr(browser_or_item, 'children') and browser_or_item.children:
                for child in browser_or_item.children:
                    item = self._find_browser_item_by_uri(child, uri, max_depth, current_depth + 1)
                    if item:
                        return item

            return None
        except Exception as e:
            self.log_message("Error finding browser item by URI: {0}".format(str(e)))
            return None
    
    # -- Browser caches and search (MCP-07) --------------------------------
    #
    # Two bounded LRU caches, both per script instance:
    #
    # * ``_uri_cache`` maps a browser URI to the live item object, so
    #   repeated lookups/loads of one URI don't re-walk the hierarchy.
    # * ``_browser_read_cache`` maps a serialized browser read - a
    #   get_browser_tree result, a get_browser_items_at_path result, a
    #   search_browser match list - to its value, so a repeated
    #   identical read returns without touching the browser at all.
    #
    # The caches live script-side because the walks they avoid happen
    # here, inside Live's process. The MCP server only ever sees
    # serialized results (a server-side cache could not hold item
    # objects at all), and Omnicorp's default direct-wire clients skip
    # the MCP server entirely.
    #
    # Invalidation: a successful load_browser_item clears both caches
    # (see _load_browser_item), and the refresh_browser_cache command
    # clears both on demand - the path a client takes after changing
    # the library from outside the script (installing a pack, editing
    # the user library, rescanning). Nothing else the script does
    # mutates browser content; reads that fail are never cached.

    def _browser_cache_state(self):
        """Return (uri_cache, read_cache), creating them on first use.

        Instances are not guaranteed to have run __init__ before a
        command arrives (the historical _uri_cache was created the
        same lazy way), so cache access goes through here.
        """
        uri_cache = getattr(self, "_uri_cache", None)
        if uri_cache is None:
            self._uri_cache = uri_cache = OrderedDict()
        read_cache = getattr(self, "_browser_read_cache", None)
        if read_cache is None:
            self._browser_read_cache = read_cache = OrderedDict()
        return uri_cache, read_cache

    @staticmethod
    def _browser_cache_get(cache, key):
        """LRU lookup: the value, or None (never a stored value)."""
        try:
            value = cache[key]
        except KeyError:
            return None
        # MCP-10: a concurrent client thread can clear or evict the
        # entry between the lookup and this touch (a load or a
        # refresh arriving on another connection). The touch is
        # best-effort: the value was read while the entry existed, so
        # the hit stands, and a vanished entry must not turn the
        # lookup into an exception.
        try:
            cache.move_to_end(key)
        except KeyError:
            pass
        return value

    @staticmethod
    def _browser_cache_put(cache, key, value, max_entries):
        cache[key] = value
        # MCP-10: the same clear/evict race as _browser_cache_get -
        # if the entry vanished between the store and the touch, the
        # concurrent clear wins (as if this put had completed just
        # before it) and the eviction loop has nothing to do.
        try:
            cache.move_to_end(key)
        except KeyError:
            return
        while len(cache) > max_entries:
            try:
                cache.popitem(last=False)
            except KeyError:
                break

    def _invalidate_browser_caches(self):
        """Drop every cached browser read and URI resolution."""
        uri_cache, read_cache = self._browser_cache_state()
        uri_cache.clear()
        read_cache.clear()

    def _refresh_browser_cache(self):
        """refresh_browser_cache command: clear both browser caches
        and report how many entries each held."""
        uri_cache, read_cache = self._browser_cache_state()
        cleared = {"uri_entries": len(uri_cache),
                   "read_entries": len(read_cache)}
        uri_cache.clear()
        read_cache.clear()
        return {"cleared": cleared}

    @staticmethod
    def _browser_roots(browser):
        """(attribute name, root item) pairs in walk order.

        The five standard categories first, then any other public
        attribute that looks like a browser item - the same set
        get_browser_tree renders.
        """
        roots = []
        for attr in ("instruments", "sounds", "drums", "audio_effects",
                     "midi_effects"):
            if hasattr(browser, attr):
                try:
                    roots.append((attr, getattr(browser, attr)))
                except (AttributeError, RuntimeError):
                    continue
        seen = set(attr for attr, _item in roots)
        for attr in dir(browser):
            if attr.startswith("_") or attr in seen:
                continue
            try:
                item = getattr(browser, attr)
            except (AttributeError, RuntimeError):
                continue
            if hasattr(item, "children") or hasattr(item, "name"):
                roots.append((attr, item))
        return roots

    def _search_browser(self, params):
        """search_browser command: find browser items by name or URI.

        Params: ``query`` (required; a case-insensitive substring
        matched against each item's name and URI), ``limit`` (default
        50, clamped to [1, MAX_BROWSER_SEARCH_PAGE_SIZE] so every
        page advances) and ``offset`` (default 0). Searching
        used to mean fetching whole subtrees and filtering
        client-side; here one walk answers every page.

        The result mirrors the agent0_live_api paging shape:
        {"query", "total", "offset", "limit", "next_offset", "items"},
        each item {"name", "is_folder", "is_device", "is_loadable",
        "uri", "path"} with ``path`` in the get_browser_items_at_path
        form (e.g. "instruments/Kits/808 Core Kit"). The full match
        list for a query is computed once and kept in the read cache,
        so paging and repeat searches cost no further walks until the
        cache is invalidated.
        """
        if not isinstance(params, dict):
            raise ValueError("search_browser requires an object of params")
        query = params.get("query")
        if not isinstance(query, (bytes, str)) or not query.strip():
            raise ValueError(
                "search_browser requires a non-empty 'query' string")
        limit = _coerce_page_int(params.get("limit"),
                                 DEFAULT_BROWSER_SEARCH_PAGE_SIZE,
                                 MAX_BROWSER_SEARCH_PAGE_SIZE,
                                 minimum=1)
        offset = _coerce_page_int(params.get("offset"), 0)

        _, read_cache = self._browser_cache_state()
        cache_key = ("search", query.lower())
        matches = self._browser_cache_get(read_cache, cache_key)
        if matches is None:
            matches = self._collect_browser_matches(query)
            self._browser_cache_put(read_cache, cache_key,
                                    matches, MAX_BROWSER_READ_CACHE_ENTRIES)
        total = len(matches)
        page = matches[offset:offset + limit]
        next_offset = offset + len(page)
        return {
            "query": query,
            "total": total,
            "offset": offset,
            "limit": limit,
            "next_offset": next_offset if next_offset < total else None,
            "items": page,
        }

    def _collect_browser_matches(self, query):
        """One full hierarchy walk collecting search matches in
        browser order (roots in _browser_roots order, depth-first)."""
        app = self.application()
        if not app:
            raise RuntimeError("Could not access Live application")
        if not hasattr(app, "browser") or app.browser is None:
            raise RuntimeError(
                "Browser is not available in the Live application")
        needle = query.lower()
        matches = []

        def visit(item, path, depth):
            children = getattr(item, "children", None) or []
            name = item.name if hasattr(item, "name") else "Unknown"
            uri = item.uri if hasattr(item, "uri") else None
            if needle in name.lower() or (uri and needle in uri.lower()):
                matches.append({
                    "name": name,
                    "is_folder": bool(children),
                    "is_device": (hasattr(item, "is_device")
                                  and item.is_device),
                    "is_loadable": (hasattr(item, "is_loadable")
                                    and item.is_loadable),
                    "uri": uri,
                    "path": path,
                })
            if depth >= 64:
                return
            for child in children:
                child_name = (child.name if hasattr(child, "name")
                              else "Unknown")
                visit(child, path + "/" + child_name, depth + 1)

        for attr, root in self._browser_roots(app.browser):
            visit(root, attr, 0)
        return matches

    # Helper methods

    def _find_blend_parameter(self, device):
        """Find Dry/Wet, Mix, or Amount on a device for Magnitude mapping."""
        preferred = ("Dry/Wet", "Dry Wet", "Mix", "Amount")
        by_name = {}
        for param in device.parameters:
            try:
                by_name[param.name] = param
            except Exception:
                continue
        for name in preferred:
            if name in by_name:
                return by_name[name], name
        # Case-insensitive fallback
        lowered = dict((k.lower(), (v, k)) for k, v in by_name.items())
        for name in preferred:
            hit = lowered.get(name.lower())
            if hit:
                return hit[0], hit[1]
        return None, None

    def _inspect_rack(self, track_index, device_index=0):
        """Inspect a rack's nested devices and blend parameters."""
        track_index = _require_index(
            track_index, "track_index", len(self._song.tracks),
            "Track index out of range")
        track = self._song.tracks[track_index]
        device_index = _require_index(
            device_index, "device_index", len(track.devices),
            "Device index out of range")
        rack = track.devices[device_index]
        if not getattr(rack, "can_have_chains", False):
            raise ValueError("Device '{0}' is not a rack".format(rack.name))

        devices_info = []
        for chain_index, chain in enumerate(rack.chains):
            for nested in chain.devices:
                blend, blend_name = self._find_blend_parameter(nested)
                param_names = []
                try:
                    param_names = [p.name for p in nested.parameters]
                except Exception:
                    pass
                devices_info.append({
                    "chain_index": chain_index,
                    "name": nested.name,
                    "class_name": nested.class_name,
                    "blend_param": blend_name,
                    "parameters": param_names,
                })

        return {
            "track_index": track_index,
            "device_index": device_index,
            "rack_name": rack.name,
            "has_macro_map": hasattr(rack, "macro_map"),
            "has_rename_macro": hasattr(rack, "rename_macro"),
            "macros_mapped": list(getattr(rack, "macros_mapped", [])),
            "devices": devices_info,
        }

    def _map_rack_magnitude(self, track_index, device_index=0, macro_name="Magnitude"):
        """Rename Macro 1 and map nested Dry/Wet (or Mix/Amount) params to it."""
        track_index = _require_index(
            track_index, "track_index", len(self._song.tracks),
            "Track index out of range")
        track = self._song.tracks[track_index]
        device_index = _require_index(
            device_index, "device_index", len(track.devices),
            "Device index out of range")
        rack = track.devices[device_index]
        if not getattr(rack, "can_have_chains", False):
            raise ValueError("Device '{0}' is not a rack".format(rack.name))
        if not hasattr(rack, "macro_map"):
            raise RuntimeError(
                "RackDevice.macro_map is unavailable in this Live version")

        # Ensure at least one macro is visible
        try:
            visible = int(getattr(rack, "visible_macro_count", 1) or 1)
            while visible < 1 and hasattr(rack, "add_macro"):
                rack.add_macro()
                visible = int(rack.visible_macro_count)
        except Exception as e:
            self.log_message("Could not adjust visible macros: {0}".format(e))

        if hasattr(rack, "rename_macro"):
            rack.rename_macro(0, macro_name)
        else:
            # Fallback: Macro 1 is usually parameters[1] (0 = Device On)
            try:
                if len(rack.parameters) > 1:
                    rack.parameters[1].name = macro_name
            except Exception:
                pass

        mapped = []
        skipped = []
        for chain_index, chain in enumerate(rack.chains):
            for nested in chain.devices:
                blend, blend_name = self._find_blend_parameter(nested)
                if not blend:
                    skipped.append({
                        "device": nested.name,
                        "reason": "no Dry/Wet, Mix, or Amount parameter",
                    })
                    continue
                try:
                    rack.macro_map(0, blend)
                    mapped.append({
                        "device": nested.name,
                        "parameter": blend_name,
                        "chain_index": chain_index,
                    })
                except Exception as e:
                    skipped.append({
                        "device": nested.name,
                        "parameter": blend_name,
                        "reason": str(e),
                    })

        return {
            "rack_name": rack.name,
            "macro_name": macro_name,
            "macro_index": 0,
            "mapped": mapped,
            "skipped": skipped,
            "macros_mapped": list(getattr(rack, "macros_mapped", [])),
        }
    
    def _get_device_type(self, device):
        """Get the type of a device"""
        try:
            # Simple heuristic - in a real implementation you'd look at the device class
            if device.can_have_drum_pads:
                return "drum_machine"
            elif device.can_have_chains:
                return "rack"
            elif "instrument" in device.class_display_name.lower():
                return "instrument"
            elif "audio_effect" in device.class_name.lower():
                return "audio_effect"
            elif "midi_effect" in device.class_name.lower():
                return "midi_effect"
            else:
                return "unknown"
        except:
            return "unknown"

    # ── Passive human-UI listeners ──────────────────────────────────────────────

    def _enqueue_passive(self, event_type, detail=None, track_index=None, clip_index=None):
        """Append a coarse human-UI event to the bounded queue.

        MCP-08: the queue is a deque capped at self._passive_max -
        appending is O(1) and overflow drops the oldest event in O(1)
        (the previous list implementation re-sliced the whole queue
        on every event once the cap was reached). Drops are counted
        in _passive_dropped and reported by the next drain. Value
        streams in PASSIVE_COALESCE_TYPES coalesce latest-value-wins
        per (type, track_index, clip_index) instead of enqueueing.
        """
        evt = {
            "type": event_type,
            "ts": time.time(),
            "track_index": track_index,
            "clip_index": clip_index,
            "detail": detail if detail is not None else {},
        }
        with self._passive_lock:
            if event_type in PASSIVE_COALESCE_TYPES:
                for pending in self._passive_events:
                    if (pending["type"] == event_type
                            and pending["track_index"] == track_index
                            and pending["clip_index"] == clip_index):
                        pending["detail"] = evt["detail"]
                        pending["ts"] = evt["ts"]
                        return
            if len(self._passive_events) >= self._passive_max:
                self._passive_events.popleft()
                self._passive_dropped = \
                    getattr(self, "_passive_dropped", 0) + 1
            self._passive_events.append(evt)

    def _drain_passive_events(self):
        """Return and clear the passive event queue (called by MCP poller).

        MCP-08: the result also carries "dropped" - events the
        bounded queue dropped since the previous drain - which is
        reset here, so a consumer can tell a quiet period from a
        flooded one.
        """
        with self._passive_lock:
            events = list(self._passive_events)
            self._passive_events.clear()
            dropped = getattr(self, "_passive_dropped", 0)
            self._passive_dropped = 0
        return {"events": events, "count": len(events), "dropped": dropped}

    def _passive_listener_count(self):
        """Total LOM listeners currently registered for passive capture."""
        total = len(getattr(self, "_song_passive_callbacks", []))
        for _track, bindings in getattr(self, "_passive_track_bindings", []):
            total += len(bindings)
        for _clip, bindings in getattr(
                self, "_passive_clip_bindings", {}).values():
            total += len(bindings)
        return total

    def _set_passive_listeners(self, enabled):
        """Register or remove the passive LOM listener census (MCP-08).

        Listeners exist only while a consumer wants the event flow:
        the MCP dataset poller enables them while dataset consent is
        on, and any client may toggle them with the
        set_passive_listeners command. Idempotent: enabling an
        enabled script (or disabling a disabled one) changes nothing.
        Returns the resulting state for the command response.
        """
        # SI-245: a real bool only - by truthiness the string
        # "false" enabled the listeners.
        enabled = _require_bool(enabled, "enabled")
        current = bool(getattr(self, "_passive_listeners_enabled", False))
        if enabled == current:
            return {
                "enabled": current,
                "changed": False,
                "listener_count": self._passive_listener_count(),
            }
        if enabled:
            self._setup_passive_listeners()
            self._passive_listeners_enabled = True
        else:
            self._teardown_passive_listeners()
            self._passive_listeners_enabled = False
        return {
            "enabled": self._passive_listeners_enabled,
            "changed": True,
            "listener_count": self._passive_listener_count(),
        }

    def _safe_add_listener(self, obj, add_name, callback):
        try:
            if obj is not None and hasattr(obj, add_name):
                getattr(obj, add_name)(callback)
                return True
        except Exception as e:
            self.log_message("add listener %s failed: %s" % (add_name, str(e)))
        return False

    def _safe_remove_listener(self, obj, remove_name, callback):
        try:
            if obj is not None and hasattr(obj, remove_name):
                getattr(obj, remove_name)(callback)
        except Exception:
            pass

    def _setup_passive_listeners(self):
        """Register high-signal LOM listeners for Mode C / assisted human edits.

        MCP-08: called only from _set_passive_listeners (a consumer
        negotiated the event flow on) or not at all - never from
        __init__.
        """
        song = self._song

        def on_tempo():
            try:
                self._enqueue_passive("tempo_changed", {"tempo": float(song.tempo)})
            except Exception:
                self._enqueue_passive("tempo_changed")

        def on_sig_num():
            try:
                self._enqueue_passive(
                    "time_signature_changed",
                    {
                        "signature_numerator": int(song.signature_numerator),
                        "signature_denominator": int(song.signature_denominator),
                    },
                )
            except Exception:
                self._enqueue_passive("time_signature_changed")

        def on_sig_den():
            on_sig_num()

        def on_is_playing():
            try:
                self._enqueue_passive(
                    "playback_changed",
                    {"is_playing": bool(song.is_playing)},
                )
            except Exception:
                self._enqueue_passive("playback_changed")

        def on_tracks():
            count = len(song.tracks)
            previous = getattr(self, "_passive_track_count", None)
            self._passive_track_count = count
            detail = {"track_count": count}
            if previous is not None:
                detail["previous_count"] = previous
                detail["removed"] = count < previous
                detail["added"] = count > previous
            self._enqueue_passive("tracks_changed", detail)
            try:
                self._rebind_track_listeners()
            except Exception as e:
                self.log_message("rebind track listeners failed: " + str(e))

        self._safe_add_listener(song, "add_tempo_listener", on_tempo)
        self._safe_add_listener(song, "add_signature_numerator_listener", on_sig_num)
        self._safe_add_listener(song, "add_signature_denominator_listener", on_sig_den)
        self._safe_add_listener(song, "add_is_playing_listener", on_is_playing)
        self._safe_add_listener(song, "add_tracks_listener", on_tracks)

        self._song_passive_callbacks = [
            ("tempo_listener", on_tempo),
            ("signature_numerator_listener", on_sig_num),
            ("signature_denominator_listener", on_sig_den),
            ("is_playing_listener", on_is_playing),
            ("tracks_listener", on_tracks),
        ]

        self._rebind_track_listeners()
        self.log_message("Passive LOM listeners registered")

    def _teardown_passive_listeners(self):
        song = getattr(self, "_song", None)
        for suffix, cb in getattr(self, "_song_passive_callbacks", []):
            self._safe_remove_listener(song, "remove_" + suffix, cb)
        self._song_passive_callbacks = []
        self._clear_track_listeners()

    def _clear_track_listeners(self):
        for track, bindings in getattr(self, "_passive_track_bindings", []):
            for add_name, callback in bindings:
                remove_name = "remove_" + add_name[len("add_"):]
                target = getattr(callback, "_passive_target", None)
                if target is not None:
                    self._safe_remove_listener(target, remove_name, callback)
                else:
                    self._safe_remove_listener(track, remove_name, callback)
        self._passive_track_bindings = []
        # MCP-08: clip bindings live in their own registry (see
        # _bind_clip_listeners); a track-level clear removes them all.
        clip_bindings = getattr(self, "_passive_clip_bindings", {})
        for key in list(clip_bindings.keys()):
            self._unbind_clip_listeners(*key)

    def _unbind_clip_listeners(self, track_index, clip_index):
        """Remove one slot's tracked clip listeners, if any (MCP-08)."""
        clip_bindings = getattr(self, "_passive_clip_bindings", None)
        if clip_bindings is None:
            self._passive_clip_bindings = clip_bindings = {}
        entry = clip_bindings.pop((track_index, clip_index), None)
        if not entry:
            return
        clip, bindings = entry
        for add_name, callback in bindings:
            remove_name = "remove_" + add_name[len("add_"):]
            target = getattr(callback, "_passive_target", None) or clip
            self._safe_remove_listener(target, remove_name, callback)

    def _rebind_track_listeners(self):
        self._clear_track_listeners()
        song = self._song
        for track_index, track in enumerate(song.tracks):
            bindings = []

            def make_track_cb(kind, t_index):
                def _cb():
                    detail = {}
                    try:
                        t = song.tracks[t_index]
                        if kind == "name_changed":
                            detail["name"] = t.name
                        elif kind == "mute_changed":
                            detail["mute"] = bool(t.mute)
                        elif kind == "solo_changed":
                            detail["solo"] = bool(t.solo)
                        elif kind == "arm_changed":
                            detail["arm"] = self._safe_arm(t)
                        elif kind == "devices_changed":
                            detail["device_count"] = len(t.devices)
                        elif kind == "volume_changed":
                            detail["volume"] = float(t.mixer_device.volume.value)
                        elif kind == "panning_changed":
                            detail["panning"] = float(t.mixer_device.panning.value)
                    except Exception:
                        pass
                    self._enqueue_passive(kind, detail, track_index=t_index)
                return _cb

            pairs = [
                ("add_name_listener", "name_changed"),
                ("add_mute_listener", "mute_changed"),
                ("add_solo_listener", "solo_changed"),
                ("add_arm_listener", "arm_changed"),
                ("add_devices_listener", "devices_changed"),
            ]
            for add_name, kind in pairs:
                cb = make_track_cb(kind, track_index)
                if self._safe_add_listener(track, add_name, cb):
                    bindings.append((add_name, cb))

            try:
                mixer = track.mixer_device
                vol_cb = make_track_cb("volume_changed", track_index)
                vol_cb._passive_target = mixer.volume
                if self._safe_add_listener(mixer.volume, "add_value_listener", vol_cb):
                    bindings.append(("add_value_listener", vol_cb))
                pan_cb = make_track_cb("panning_changed", track_index)
                pan_cb._passive_target = mixer.panning
                if self._safe_add_listener(mixer.panning, "add_value_listener", pan_cb):
                    bindings.append(("add_value_listener", pan_cb))
            except Exception as e:
                self.log_message("mixer listeners failed on track %d: %s" % (track_index, str(e)))

            try:
                for clip_index, slot in enumerate(track.clip_slots):
                    def make_slot_cb(t_index, c_index):
                        def _cb():
                            has = False
                            try:
                                has = bool(song.tracks[t_index].clip_slots[c_index].has_clip)
                            except Exception:
                                pass
                            self._enqueue_passive(
                                "clip_slot_changed",
                                {"has_clip": has},
                                track_index=t_index,
                                clip_index=c_index,
                            )
                            try:
                                self._bind_clip_listeners(t_index, c_index)
                            except Exception:
                                pass
                        return _cb

                    slot_cb = make_slot_cb(track_index, clip_index)
                    slot_cb._passive_target = slot
                    if self._safe_add_listener(slot, "add_has_clip_listener", slot_cb):
                        bindings.append(("add_has_clip_listener", slot_cb))
                    if slot.has_clip:
                        self._bind_clip_listeners(track_index, clip_index, bindings)
            except Exception as e:
                self.log_message("clip slot listeners failed on track %d: %s" % (track_index, str(e)))

            self._passive_track_bindings.append((track, bindings))

    def _bind_clip_listeners(self, track_index, clip_index, bindings=None):
        # MCP-08 (leak fix): bindings are tracked in
        # _passive_clip_bindings, keyed by slot, and any previous set
        # for the slot is removed before binding anew. Historically
        # the slot callback called this without the bindings list, so
        # its listeners were recorded nowhere: every slot toggle added
        # three more clip callbacks that teardown never removed. The
        # bindings parameter is still accepted (the rebind loop passes
        # its per-track list) but storage is always the registry, so
        # both call paths are tracked exactly once.
        self._unbind_clip_listeners(track_index, clip_index)
        try:
            track = self._song.tracks[track_index]
            slot = track.clip_slots[clip_index]
            if not slot.has_clip:
                return
            clip = slot.clip

            def on_name():
                name = ""
                try:
                    name = clip.name
                except Exception:
                    pass
                self._enqueue_passive(
                    "clip_name_changed",
                    {"name": name},
                    track_index=track_index,
                    clip_index=clip_index,
                )

            def on_notes():
                self._enqueue_passive(
                    "clip_notes_changed",
                    {},
                    track_index=track_index,
                    clip_index=clip_index,
                )

            def on_playing():
                playing = False
                try:
                    playing = bool(clip.is_playing)
                except Exception:
                    pass
                self._enqueue_passive(
                    "clip_playing_changed",
                    {"is_playing": playing},
                    track_index=track_index,
                    clip_index=clip_index,
                )

            bound = []
            for add_name, cb in [
                ("add_name_listener", on_name),
                ("add_notes_listener", on_notes),
                ("add_playing_status_listener", on_playing),
            ]:
                cb._passive_target = clip
                if self._safe_add_listener(clip, add_name, cb):
                    bound.append((add_name, cb))
            if bound:
                clip_bindings = getattr(self, "_passive_clip_bindings", None)
                if clip_bindings is None:
                    self._passive_clip_bindings = clip_bindings = {}
                clip_bindings[(track_index, clip_index)] = (clip, bound)
        except Exception as e:
            self.log_message(
                "bind clip listeners %d/%d failed: %s"
                % (track_index, clip_index, str(e))
            )

    # ── Dataset / state snapshot helpers ──────────────────────────────────────

    def _safe_attr(self, obj, attr, cast=None, default=None):
        try:
            val = getattr(obj, attr)
            if callable(val):
                return default
            if cast is not None:
                return cast(val)
            return val
        except Exception:
            return default

    def _notes_from_clip(self, clip):
        """Extract MIDI notes from a clip (incl. MPE/expression when available)."""
        notes = []
        if not clip or not getattr(clip, "is_midi_clip", False):
            return notes

        if hasattr(clip, "get_notes_extended"):
            try:
                raw = clip.get_notes_extended(0, 128, 0.0, float(clip.length) + 1.0)
                for n in raw:
                    entry = {
                        "pitch": int(getattr(n, "pitch", 0)),
                        "start_time": float(getattr(n, "start_time", 0.0)),
                        "duration": float(getattr(n, "duration", 0.0)),
                        "velocity": float(getattr(n, "velocity", 0)),
                        "mute": bool(getattr(n, "mute", False)),
                    }
                    for opt, caster in [
                        ("probability", float),
                        ("velocity_deviation", float),
                        ("release_velocity", float),
                        ("note_id", int),
                    ]:
                        if hasattr(n, opt):
                            try:
                                entry[opt] = caster(getattr(n, opt))
                            except Exception:
                                pass
                    for opt in ("pitch_bend_range", "pressure", "timbre", "slide"):
                        if hasattr(n, opt):
                            try:
                                entry[opt] = float(getattr(n, opt))
                            except Exception:
                                pass
                    notes.append(entry)
                return notes
            except Exception as e:
                self.log_message("get_notes_extended failed, falling back: " + str(e))

        if hasattr(clip, "get_notes"):
            try:
                raw = clip.get_notes(0.0, 0, float(clip.length) + 1.0, 128)
                for n in raw:
                    notes.append({
                        "pitch": int(n[0]),
                        "start_time": float(n[1]),
                        "duration": float(n[2]),
                        "velocity": float(n[3]),
                        "mute": bool(n[4]) if len(n) > 4 else False,
                    })
            except Exception as e:
                self.log_message("get_notes failed: " + str(e))
        return notes

    def _warp_markers_from_clip(self, clip):
        markers = []
        try:
            raw = getattr(clip, "warp_markers", None)
            if not raw:
                return markers
            for m in raw:
                markers.append({
                    "beat_time": float(getattr(m, "beat_time", getattr(m, "time", 0.0))),
                    "sample_time": float(
                        getattr(m, "sample_time", getattr(m, "time", 0.0))
                    ),
                })
        except Exception as e:
            self.log_message("warp_markers read failed: " + str(e))
        return markers

    def _automated_params_for_device(self, device):
        automated = []
        try:
            for param in device.parameters:
                is_auto = False
                try:
                    if hasattr(param, "automation_state"):
                        is_auto = int(param.automation_state) != 0
                    elif hasattr(param, "is_automated"):
                        is_auto = bool(param.is_automated)
                except Exception:
                    continue
                if is_auto:
                    automated.append(param.name)
        except Exception:
            pass
        return automated

    # Racks nest, and a pathological project could nest deeply. Cap the walk so
    # a snapshot can never blow the stack or the payload size.
    _MAX_CHAIN_DEPTH = 4

    def _serialize_device(self, device, device_index, include_params=True,
                          include_automation=None, depth=0):
        info = {
            "index": device_index,
            "name": device.name,
            "class_name": device.class_name,
            "type": self._get_device_type(device),
        }
        # MCP-05: the automated-parameter scan walks every parameter of
        # the device, and its result feeds only the two keys below -- so
        # it runs only when those keys will be emitted. By default that
        # is exactly when include_params is on; an explicit
        # include_automation overrides either way. When the scan does
        # not run, both keys are omitted entirely.
        scan_automation = include_params if include_automation is None \
            else bool(include_automation)
        if scan_automation:
            automated = self._automated_params_for_device(device)
            if automated:
                info["automated_parameters"] = automated
                info["automation_enabled"] = True
            else:
                info["automation_enabled"] = False

        if include_params:
            params = []
            try:
                for p_index, param in enumerate(device.parameters):
                    try:
                        entry = {
                            "index": p_index,
                            "name": param.name,
                            "value": float(param.value),
                            "min": float(param.min),
                            "max": float(param.max),
                            "is_enabled": bool(getattr(param, "is_enabled", True)),
                            "is_quantized": bool(getattr(param, "is_quantized", False)),
                        }
                        if hasattr(param, "value_string"):
                            entry["value_string"] = str(param.value_string)
                        if hasattr(param, "automation_state"):
                            try:
                                entry["automation_state"] = int(param.automation_state)
                            except Exception:
                                pass
                        params.append(entry)
                    except Exception:
                        continue
            except Exception as e:
                self.log_message("Error reading device parameters: " + str(e))
            info["parameters"] = params

        # Devices inside a rack carry the actual sound design — a drum rack's
        # nested Operator, an instrument rack's filter. Without this walk a rack
        # contributes only its 8 macros and the timbral state is invisible.
        if getattr(device, "can_have_chains", False):
            if depth >= self._MAX_CHAIN_DEPTH:
                info["chains_truncated"] = True
            else:
                info["chains"] = self._serialize_chains(
                    device, include_params=include_params,
                    include_automation=include_automation, depth=depth
                )
        return info

    def _serialize_chains(self, rack, include_params=True,
                          include_automation=None, depth=0):
        chains = []
        try:
            chain_lists = [("chains", getattr(rack, "chains", []))]
            returns = getattr(rack, "return_chains", None)
            if returns:
                chain_lists.append(("return_chains", returns))

            for kind, chain_list in chain_lists:
                for chain_index, chain in enumerate(chain_list):
                    entry = {
                        "index": chain_index,
                        "kind": kind,
                        "chain_name": self._safe_attr(chain, "name", str, ""),
                        "mute": bool(self._safe_attr(chain, "mute", bool, False)),
                        "solo": bool(self._safe_attr(chain, "solo", bool, False)),
                    }
                    try:
                        mixer = chain.mixer_device
                        entry["volume"] = float(mixer.volume.value)
                        entry["panning"] = float(mixer.panning.value)
                    except Exception:
                        pass

                    # Drum racks expose the pad's note, which is what ties a
                    # nested device back to the kick/snare/hat it voices.
                    note = self._safe_attr(chain, "out_note", int, None)
                    if note is not None:
                        entry["out_note"] = note

                    nested = []
                    try:
                        for d_i, dev in enumerate(chain.devices):
                            nested.append(
                                self._serialize_device(
                                    dev,
                                    d_i,
                                    include_params=include_params,
                                    include_automation=include_automation,
                                    depth=depth + 1,
                                )
                            )
                    except Exception as e:
                        self.log_message("Error reading chain devices: " + str(e))
                    entry["devices"] = nested
                    chains.append(entry)
        except Exception as e:
            self.log_message("Error serializing rack chains: " + str(e))
        return chains

    def _serialize_clip_common(self, clip):
        info = {
            "looping": bool(self._safe_attr(clip, "looping", bool, False)),
            "loop_start": self._safe_attr(clip, "loop_start", float, None),
            "loop_end": self._safe_attr(clip, "loop_end", float, None),
            "warping": bool(self._safe_attr(clip, "warping", bool, False)),
            "warp_mode": self._safe_attr(clip, "warp_mode", int, None),
            "gain": self._safe_attr(clip, "gain", float, None),
            "pitch_coarse": self._safe_attr(clip, "pitch_coarse", int, None),
            "pitch_fine": self._safe_attr(clip, "pitch_fine", int, None),
            "launch_mode": self._safe_attr(clip, "launch_mode", int, None),
        }
        for attr in ("file_path", "file_path_relative"):
            path = self._safe_attr(clip, attr, str, None)
            if path:
                info["file_path"] = path
                break
        markers = self._warp_markers_from_clip(clip)
        if markers:
            info["warp_markers"] = markers
            info["warp_marker_count"] = len(markers)
        return dict((k, v) for k, v in info.items() if v is not None)

    def _serialize_session_clip(self, clip, include_notes=True):
        info = {
            "name": clip.name,
            "length": float(clip.length),
            "is_playing": bool(clip.is_playing),
            "is_recording": bool(getattr(clip, "is_recording", False)),
            "is_midi_clip": bool(getattr(clip, "is_midi_clip", False)),
            "is_audio_clip": bool(getattr(clip, "is_audio_clip", False)),
            "color": int(getattr(clip, "color", 0)),
        }
        info.update(self._serialize_clip_common(clip))
        if include_notes and info["is_midi_clip"]:
            info["notes"] = self._notes_from_clip(clip)
            info["note_count"] = len(info["notes"])
        return info

    def _serialize_arrangement_clip(self, clip, include_notes=True):
        info = {
            "name": clip.name,
            "start_time": float(clip.start_time),
            "end_time": float(clip.end_time),
            "length": float(clip.length),
            "color": int(getattr(clip, "color", 0)),
            "is_midi_clip": bool(getattr(clip, "is_midi_clip", False)),
            "is_audio_clip": bool(getattr(clip, "is_audio_clip", False)),
            "is_playing": bool(getattr(clip, "is_playing", False)),
        }
        info.update(self._serialize_clip_common(clip))
        if include_notes and info["is_midi_clip"]:
            info["notes"] = self._notes_from_clip(clip)
            info["note_count"] = len(info["notes"])
        return info

    def _serialize_sends(self, track):
        sends = []
        try:
            for i, send in enumerate(track.mixer_device.sends):
                sends.append({
                    "index": i,
                    "value": float(send.value),
                    "name": str(getattr(send, "name", "Send %d" % i)),
                })
        except Exception:
            pass
        return sends

    def _serialize_scenes(self):
        scenes = []
        try:
            for i, scene in enumerate(self._song.scenes):
                scenes.append({
                    "index": i,
                    "name": str(scene.name),
                    "tempo": self._safe_attr(scene, "tempo", float, None),
                    "is_triggered": bool(self._safe_attr(scene, "is_triggered", bool, False)),
                })
        except Exception as e:
            self.log_message("scenes serialize failed: " + str(e))
        return scenes

    def _serialize_cue_points(self):
        cues = []
        try:
            for cue in self._song.cue_points:
                cues.append({
                    "name": str(getattr(cue, "name", "")),
                    "time": float(getattr(cue, "time", 0.0)),
                })
        except Exception as e:
            self.log_message("cue_points serialize failed: " + str(e))
        return cues

    def _serialize_return_tracks(self, include_params=True,
                                 include_automation=None):
        returns = []
        try:
            for i, track in enumerate(self._song.return_tracks):
                devices = []
                for d_i, device in enumerate(track.devices):
                    devices.append(
                        self._serialize_device(device, d_i, include_params=include_params,
                                       include_automation=include_automation)
                    )
                returns.append({
                    "index": i,
                    "name": track.name,
                    "mute": bool(track.mute),
                    "solo": bool(track.solo),
                    "volume": float(track.mixer_device.volume.value),
                    "panning": float(track.mixer_device.panning.value),
                    "devices": devices,
                })
        except Exception as e:
            self.log_message("return_tracks serialize failed: " + str(e))
        return returns

    def _serialize_master_track(self, include_params=True,
                                include_automation=None):
        """Master chain — the bus compressor/limiter that shapes the final sound."""
        try:
            track = self._song.master_track
            devices = []
            for d_i, device in enumerate(track.devices):
                devices.append(
                    self._serialize_device(device, d_i, include_params=include_params,
                                       include_automation=include_automation)
                )
            return {
                "volume": float(track.mixer_device.volume.value),
                "panning": float(track.mixer_device.panning.value),
                "devices": devices,
            }
        except Exception as e:
            self.log_message("master_track serialize failed: " + str(e))
            return None

    def _get_clip_notes(self, track_index, clip_index):
        try:
            track_index = _require_index(
                track_index, "track_index", len(self._song.tracks),
                "Track index out of range")
            track = self._song.tracks[track_index]
            clip_index = _require_index(
                clip_index, "clip_index", len(track.clip_slots),
                "Clip index out of range")
            slot = track.clip_slots[clip_index]
            if not slot.has_clip:
                raise Exception("No clip in slot")
            clip = slot.clip
            if not getattr(clip, "is_midi_clip", False):
                raise Exception("Clip is not a MIDI clip")
            notes = self._notes_from_clip(clip)
            return {
                "track_index": track_index,
                "clip_index": clip_index,
                "clip_name": clip.name,
                "length": float(clip.length),
                "note_count": len(notes),
                "notes": notes,
            }
        except Exception as e:
            self.log_message("Error getting clip notes: " + str(e))
            raise

    def _get_device_parameters(self, track_index, device_index,
                               parameter_indices=None, parameter_names=None):
        """Read one device's parameters, optionally a subset (MCP-06).

        With neither selector given this is exactly the pre-subset
        read. With ``parameter_indices`` and/or ``parameter_names``
        given, every ``parameters`` list in the returned device tree
        (the device itself and any devices nested in its chains) keeps
        only the entries whose index or name was requested - the union
        when both selectors are given; selectors that match nothing
        simply contribute nothing. Per-entry shape is unchanged.
        """
        try:
            track_index = _require_index(
                track_index, "track_index", len(self._song.tracks),
                "Track index out of range")
            track = self._song.tracks[track_index]
            device_index = _require_index(
                device_index, "device_index", len(track.devices),
                "Device index out of range")
            device = track.devices[device_index]
            info = self._serialize_device(device, device_index, include_params=True)
            if parameter_indices is not None or parameter_names is not None:
                wanted_indices = set()
                if parameter_indices is not None:
                    for entry in _require_list_selector(
                            parameter_indices, "parameter_indices"):
                        # SI-241: only a real int selects (the type
                        # check mirrors _is_valid_request_id, so bools
                        # are excluded); every other entry is unusable
                        # and skipped, exactly as "x"/None/[[1]] were
                        # skipped before and as +-Inf/NaN are skipped
                        # under the SI-240 catch retained below - a
                        # float is never truncated into an index and
                        # True never selects index 1.
                        if type(entry) is not int:
                            continue
                        try:
                            wanted_indices.add(int(entry))
                        except (TypeError, ValueError, OverflowError):
                            continue
                wanted_names = set()
                if parameter_names is not None:
                    for entry in _require_list_selector(
                            parameter_names, "parameter_names"):
                        # SI-241: a name entry that is not a string
                        # can never match a parameter name; refuse it
                        # by name instead of the raw "unhashable type"
                        # a list/dict entry raised from set(...)
                        # before, or the silent no-match a hashable
                        # non-string entry gave.
                        if not isinstance(entry, str):
                            raise ValueError(
                                "Invalid parameter_names: "
                                "entries must be strings")
                        wanted_names.add(entry)
                info = self._filter_device_parameters(
                    info, wanted_indices, wanted_names)
            return {
                "track_index": track_index,
                "device": info,
            }
        except Exception as e:
            self.log_message("Error getting device parameters: " + str(e))
            raise

    def _filter_device_parameters(self, info, wanted_indices, wanted_names):
        """Restrict every ``parameters`` list in a serialised device
        tree to the entries whose index or name was requested (MCP-06
        subset reads). Entry dicts are kept whole, in song order."""
        params = info.get("parameters")
        if isinstance(params, list):
            info["parameters"] = [
                entry for entry in params
                if entry.get("index") in wanted_indices
                or entry.get("name") in wanted_names
            ]
        for chain in info.get("chains") or ():
            for nested in chain.get("devices") or ():
                self._filter_device_parameters(
                    nested, wanted_indices, wanted_names)
        return info

    def _get_session_snapshot(self, include_notes=True, include_params=True,
                              track_indices=None, include_scenes=True,
                              include_return_tracks=True,
                              include_master_track=True,
                              include_cue_points=True,
                              include_clip_slots=True, include_devices=True,
                              include_arrangement_clips=True,
                              include_automation=None):
        """Full v2 project state dump for trajectory dataset recording.

        MCP-05 projection -- every parameter is opt-in and defaults to
        the pre-projection behaviour:
        - track_indices restricts the tracks list to those song indices
          (invalid entries ignored, song order preserved);
        - include_scenes / include_return_tracks / include_master_track /
          include_cue_points = False omits that top-level section;
        - include_clip_slots / include_devices /
          include_arrangement_clips = False omits that per-track section;
        - include_automation overrides whether the automated-parameter
          scan runs (default: follows include_params; see
          _serialize_device).
        An omitted section is not serialised at all.
        """
        try:
            # SI-245: projection flags are real bools. Read by
            # truthiness, the string "false" included the section
            # it was sent to omit (and reported include_notes /
            # include_params back as true). include_automation is
            # the one exception in shape, not in strictness: None
            # is its documented no-override sentinel (the omitted
            # behaviour, following include_params), so None passes
            # and every other non-bool refuses by name.
            include_notes = _require_bool(include_notes, "include_notes")
            include_params = _require_bool(
                include_params, "include_params")
            include_scenes = _require_bool(
                include_scenes, "include_scenes")
            include_return_tracks = _require_bool(
                include_return_tracks, "include_return_tracks")
            include_master_track = _require_bool(
                include_master_track, "include_master_track")
            include_cue_points = _require_bool(
                include_cue_points, "include_cue_points")
            include_clip_slots = _require_bool(
                include_clip_slots, "include_clip_slots")
            include_devices = _require_bool(
                include_devices, "include_devices")
            include_arrangement_clips = _require_bool(
                include_arrangement_clips, "include_arrangement_clips")
            if include_automation is not None:
                include_automation = _require_bool(
                    include_automation, "include_automation")
            session = self._get_session_info()
            if track_indices is None:
                selected = list(range(len(self._song.tracks)))
            else:
                wanted = set()
                for entry in _require_list_selector(
                        track_indices, "track_indices"):
                    # SI-241: the same rule as parameter_indices -
                    # only a real int selects (bools excluded); every
                    # other entry is unusable and skipped, never
                    # truncated or coerced, with the SI-240 catch
                    # retained for the conversion itself.
                    if type(entry) is not int:
                        continue
                    try:
                        wanted.add(int(entry))
                    except (TypeError, ValueError, OverflowError):
                        continue
                selected = [i for i in range(len(self._song.tracks))
                            if i in wanted]
            tracks = []
            for track_index in selected:
                track = self._song.tracks[track_index]
                entry = {
                    "index": track_index,
                    "name": track.name,
                    "is_audio_track": bool(track.has_audio_input),
                    "is_midi_track": bool(track.has_midi_input),
                    "mute": bool(track.mute),
                    "solo": bool(track.solo),
                    "arm": self._safe_arm(track),
                    "volume": float(track.mixer_device.volume.value),
                    "panning": float(track.mixer_device.panning.value),
                    "sends": self._serialize_sends(track),
                }

                if include_clip_slots:
                    clip_slots = []
                    for slot_index, slot in enumerate(track.clip_slots):
                        clip_info = None
                        if slot.has_clip:
                            clip_info = self._serialize_session_clip(
                                slot.clip, include_notes=include_notes
                            )
                        clip_slots.append({
                            "index": slot_index,
                            "has_clip": bool(slot.has_clip),
                            "clip": clip_info,
                        })
                    entry["clip_slots"] = clip_slots

                if include_devices:
                    devices = []
                    for device_index, device in enumerate(track.devices):
                        devices.append(
                            self._serialize_device(
                                device, device_index,
                                include_params=include_params,
                                include_automation=include_automation
                            )
                        )
                    entry["devices"] = devices

                if include_arrangement_clips:
                    arrangement_clips = []
                    try:
                        for clip in track.arrangement_clips:
                            arrangement_clips.append(
                                self._serialize_arrangement_clip(
                                    clip, include_notes=include_notes
                                )
                            )
                    except Exception as e:
                        self.log_message(
                            "arrangement_clips unavailable on track %d: %s"
                            % (track_index, str(e))
                        )
                    entry["arrangement_clips"] = arrangement_clips

                tracks.append(entry)

            result = {
                "schema": "ableton_mcp_snapshot_v2",
                "session": session,
                "tracks": tracks,
            }
            if include_scenes:
                result["scenes"] = self._serialize_scenes()
            if include_return_tracks:
                result["return_tracks"] = self._serialize_return_tracks(
                    include_params=include_params,
                    include_automation=include_automation
                )
            if include_master_track:
                result["master_track"] = self._serialize_master_track(
                    include_params=include_params,
                    include_automation=include_automation
                )
            if include_cue_points:
                result["cue_points"] = self._serialize_cue_points()
            result["include_notes"] = bool(include_notes)
            result["include_params"] = bool(include_params)
            return result
        except Exception as e:
            self.log_message("Error getting session snapshot: " + str(e))
            raise
    def _set_device_parameter(self, track_index, device_index, parameter_index, value):
        try:
            track_index = _require_index(
                track_index, "track_index", len(self._song.tracks),
                "Track index out of range")
            track = self._song.tracks[track_index]
            device_index = _require_index(
                device_index, "device_index", len(track.devices),
                "Device index out of range")
            device = track.devices[device_index]
            parameter_index = _require_index(
                parameter_index, "parameter_index", len(device.parameters),
                "Parameter index out of range")
            param = device.parameters[parameter_index]
            old = float(param.value)
            param.value = _require_finite_number(value, "value")
            return {
                "track_index": track_index,
                "device_index": device_index,
                "parameter_index": parameter_index,
                "name": param.name,
                "old_value": old,
                "value": float(param.value),
                "min": float(param.min),
                "max": float(param.max),
            }
        except Exception as e:
            self.log_message("Error setting device parameter: " + str(e))
            raise

    def _set_device_parameters(self, params, timing_state=None):
        """Apply a list of parameter writes to ONE device (MCP-06).

        The wire command ``set_device_parameters`` takes
        ``track_index``, ``device_index`` and ``parameters``: a list of
        items, each ``{"parameter_index": i, "value": v}`` - an item
        may address by ``name`` instead of ``parameter_index`` (the
        name is resolved against the device's parameters first, then
        set by index), and ``parameter_index`` wins when both are
        given. An optional ``stop_on_error`` (default False) mirrors
        execute_batch.

        Every item is applied through _set_device_parameter itself, so
        clamping, value conversion and error conditions are the single
        set's, item for item, and each item that reaches the set
        produces exactly the envelope a direct set_device_parameter
        call would have produced: ``{"status": "success",
        "result": {...}}`` (the single set's own result, old/new values
        included) or ``{"status": "error", "result": {},
        "message": ...}``. Items that never reach the set (malformed,
        unaddressable, unknown name) get batch-style ``{"status":
        "error", "message": ...}`` markers. One bad item does not
        block the others; with stop_on_error the remaining items are
        reported as ``{"status": "skipped", ...}`` - never silently
        dropped. The success result is ``{"results": [...],
        "summary": {"total": n, "succeeded": n, "failed": n,
        "skipped": n}}``, mirroring execute_batch.

        This is a sequencing primitive, NOT a transaction: items apply
        in order and a failed item does not roll back the items before
        it. The whole list runs inside ONE schedule_message hop.
        """
        if not isinstance(params, dict):
            return {"status": "error",
                    "message": "set_device_parameters requires an object of params"}
        items = params.get("parameters")
        if not isinstance(items, list):
            return {"status": "error",
                    "message": "set_device_parameters requires a 'parameters' list"}
        if len(items) > MAX_BULK_PARAMETERS:
            return {"status": "error",
                    "message": "set_device_parameters accepts at most %d parameter items"
                               % MAX_BULK_PARAMETERS}
        track_index = params.get("track_index", 0)
        device_index = params.get("device_index", 0)
        # SI-245: stop_on_error is a real bool when sent (omitted
        # keeps the default False), as in execute_batch - by
        # truthiness the string "false" stopped on the first
        # error. Refused in this method's own early-guard style.
        if "stop_on_error" in params:
            try:
                stop_on_error = _require_bool(
                    params.get("stop_on_error"), "stop_on_error")
            except ValueError as e:
                return {"status": "error", "message": str(e)}
        else:
            stop_on_error = False

        response_queue = queue.Queue()

        def bulk_task():
            results = []
            succeeded = 0
            failed = 0
            skipped = 0
            try:
                halted = False
                for item in items:
                    if halted:
                        results.append({
                            "status": "skipped",
                            "message": "Skipped: stop_on_error after "
                                       "a failed item",
                        })
                        skipped += 1
                        continue
                    item_response = self._apply_parameter_item(
                        track_index, device_index, item)
                    results.append(item_response)
                    if item_response.get("status") == "error":
                        failed += 1
                        if stop_on_error:
                            halted = True
                    else:
                        succeeded += 1
            except Exception as e:
                self.log_message("Error in bulk parameter task: " + str(e))
                self.log_message(traceback.format_exc())
                response_queue.put({"status": "error", "message": str(e)})
                return
            response_queue.put({"status": "success", "result": {
                "results": results,
                "summary": {"total": len(items),
                            "succeeded": succeeded,
                            "failed": failed,
                            "skipped": skipped},
            }})

        self._schedule_main_thread_task(bulk_task, timing_state)

        # The list may legitimately take as long as its items could
        # have taken one by one: mirror the batch queue budget.
        try:
            return response_queue.get(timeout=max(10.0 * len(items), 10.0))
        except queue.Empty:
            return {"status": "error",
                    "message": "Timeout waiting for bulk parameter set to complete"}

    def _apply_parameter_item(self, track_index, device_index, item):
        """Apply one set_device_parameters item via the single set.

        An item that reaches the set gets exactly the envelope a
        direct set_device_parameter call for the same write would
        produce: the single set's own result on success, its error
        envelope - ``"result": {}`` included - on failure. Name
        resolution is the only pre-step: an item with a ``name`` and
        no ``parameter_index`` is resolved to that parameter's index,
        then set by index like any direct call. An item that never
        reaches the set (malformed, no usable address, name
        resolution failing) is a per-item error in the batch style -
        ``{"status": "error", "message": ...}`` carrying the same
        message a direct call would produce, without the dispatched
        call's ``"result": {}`` key - never a silent no-op. A
        missing ``value`` takes the single set's own dispatcher
        default (0.0).
        """
        if not isinstance(item, dict):
            return {"status": "error",
                    "message": "Parameter items must be objects with "
                               "'parameter_index' (or 'name') and 'value'"}
        parameter_index = item.get("parameter_index")
        if parameter_index is None:
            name = item.get("name")
            if name is None:
                return {"status": "error",
                        "message": "Parameter item requires "
                                   "'parameter_index' or 'name'"}
            try:
                parameter_index = self._parameter_index_by_name(
                    track_index, device_index, name)
            except Exception as e:
                return {"status": "error", "message": str(e)}
        try:
            result = self._set_device_parameter(
                track_index, device_index, parameter_index,
                item.get("value", 0.0))
            return {"status": "success", "result": result}
        except Exception as e:
            # Exactly the envelope a direct set_device_parameter call
            # produces for the same failure: the dispatcher initialises
            # "result" before routing and leaves it in place on error.
            return {"status": "error", "result": {}, "message": str(e)}

    def _parameter_index_by_name(self, track_index, device_index, name):
        """Index of the device parameter called ``name`` (first match).

        Raises with the single set's own messages when the track or
        device index is out of range, so a name-addressed item fails
        with the same message the equivalent direct call would
        produce. The failure surfaces before the set, though, so the
        item's envelope is the pre-set batch-style per-item error -
        that message without the ``"result": {}`` key a dispatched
        direct call carries; only items that reach the set return
        the direct call's envelope exactly.
        """
        track_index = _require_index(
            track_index, "track_index", len(self._song.tracks),
            "Track index out of range")
        track = self._song.tracks[track_index]
        device_index = _require_index(
            device_index, "device_index", len(track.devices),
            "Device index out of range")
        device = track.devices[device_index]
        for index, param in enumerate(device.parameters):
            if param.name == name:
                return index
        raise ValueError("Parameter not found: " + str(name))

    def get_browser_tree(self, category_type="all"):
        """
        Get a simplified tree of browser categories.
        
        Args:
            category_type: Type of categories to get ('all', 'instruments', 'sounds', etc.)
            
        Returns:
            Dictionary with the browser tree structure
        """
        try:
            # Access the application's browser instance instead of creating a new one
            app = self.application()
            if not app:
                raise RuntimeError("Could not access Live application")
                
            # Check if browser is available
            if not hasattr(app, 'browser') or app.browser is None:
                raise RuntimeError("Browser is not available in the Live application")

            # MCP-07: a repeated identical tree read is served from
            # the read cache without re-walking the browser; the cache
            # is invalidated on load and on refresh_browser_cache.
            _, read_cache = self._browser_cache_state()
            # SI-237: refuse an unhashable category_type by name
            # before it can crash the cache lookup as a raw TypeError.
            _require_hashable_cache_key(category_type, "category_type")
            cache_key = ("tree", category_type)
            cached = self._browser_cache_get(read_cache, cache_key)
            if cached is not None:
                return cached

            # The attribute list feeds the result's
            # available_categories and the extra-category scan below.
            # It used to also be logged on every call - a full dir()
            # dump per read (MCP-07 logging diet).
            browser_attrs = [attr for attr in dir(app.browser) if not attr.startswith('_')]

            result = {
                "type": category_type,
                "categories": [],
                "available_categories": browser_attrs
            }
            
            # The full browser is far too large to serialise, so descend one
            # level and mark anything deeper with has_more. The MCP server
            # renders that as "[...]" and get_browser_items_at_path walks on
            # from there.
            max_depth = 1
            max_children = 64
            folder_count = [0]

            def process_item(item, depth=0):
                if not item:
                    return None

                children = getattr(item, 'children', None) or []
                is_folder = bool(children)
                if is_folder:
                    folder_count[0] += 1

                node = {
                    "name": item.name if hasattr(item, 'name') else "Unknown",
                    "is_folder": is_folder,
                    "is_device": hasattr(item, 'is_device') and item.is_device,
                    "is_loadable": hasattr(item, 'is_loadable') and item.is_loadable,
                    "uri": item.uri if hasattr(item, 'uri') else None,
                    "children": []
                }

                if is_folder and depth >= max_depth:
                    node["has_more"] = True
                    return node

                for child in children[:max_children]:
                    try:
                        processed = process_item(child, depth + 1)
                    except Exception as e:
                        self.log_message("Error processing browser child: {0}".format(str(e)))
                        continue
                    if processed:
                        node["children"].append(processed)
                if len(children) > max_children:
                    node["has_more"] = True

                return node

            # Process based on category type and available attributes
            if (category_type == "all" or category_type == "instruments") and hasattr(app.browser, 'instruments'):
                try:
                    instruments = process_item(app.browser.instruments)
                    if instruments:
                        instruments["name"] = "Instruments"  # Ensure consistent naming
                        result["categories"].append(instruments)
                except Exception as e:
                    self.log_message("Error processing instruments: {0}".format(str(e)))
            
            if (category_type == "all" or category_type == "sounds") and hasattr(app.browser, 'sounds'):
                try:
                    sounds = process_item(app.browser.sounds)
                    if sounds:
                        sounds["name"] = "Sounds"  # Ensure consistent naming
                        result["categories"].append(sounds)
                except Exception as e:
                    self.log_message("Error processing sounds: {0}".format(str(e)))
            
            if (category_type == "all" or category_type == "drums") and hasattr(app.browser, 'drums'):
                try:
                    drums = process_item(app.browser.drums)
                    if drums:
                        drums["name"] = "Drums"  # Ensure consistent naming
                        result["categories"].append(drums)
                except Exception as e:
                    self.log_message("Error processing drums: {0}".format(str(e)))
            
            if (category_type == "all" or category_type == "audio_effects") and hasattr(app.browser, 'audio_effects'):
                try:
                    audio_effects = process_item(app.browser.audio_effects)
                    if audio_effects:
                        audio_effects["name"] = "Audio Effects"  # Ensure consistent naming
                        result["categories"].append(audio_effects)
                except Exception as e:
                    self.log_message("Error processing audio_effects: {0}".format(str(e)))
            
            if (category_type == "all" or category_type == "midi_effects") and hasattr(app.browser, 'midi_effects'):
                try:
                    midi_effects = process_item(app.browser.midi_effects)
                    if midi_effects:
                        midi_effects["name"] = "MIDI Effects"
                        result["categories"].append(midi_effects)
                except Exception as e:
                    self.log_message("Error processing midi_effects: {0}".format(str(e)))
            
            # Try to process other potentially available categories
            for attr in browser_attrs:
                if attr not in ['instruments', 'sounds', 'drums', 'audio_effects', 'midi_effects'] and \
                   (category_type == "all" or category_type == attr):
                    try:
                        item = getattr(app.browser, attr)
                        if hasattr(item, 'children') or hasattr(item, 'name'):
                            category = process_item(item)
                            if category:
                                category["name"] = attr.capitalize()
                                result["categories"].append(category)
                    except Exception as e:
                        self.log_message("Error processing {0}: {1}".format(attr, str(e)))
            
            # The MCP server reports this back to the caller; without it the
            # header always claimed zero folders.
            result["total_folders"] = folder_count[0]

            self.log_message("Browser tree generated for {0} with {1} root categories".format(
                category_type, len(result['categories'])))
            self._browser_cache_put(read_cache, cache_key, result,
                                    MAX_BROWSER_READ_CACHE_ENTRIES)
            return result
            
        except Exception as e:
            self.log_message("Error getting browser tree: {0}".format(str(e)))
            self.log_message(traceback.format_exc())
            raise
    
    def get_browser_items_at_path(self, path, limit=None, offset=None):
        """
        Get browser items at a specific path.
        
        Args:
            path: Path in the format "category/folder/subfolder"
                 where category is one of: instruments, sounds, drums, audio_effects, midi_effects
                 or any other available browser category
                 
        Returns:
            Dictionary with items at the specified path. With the
            opt-in limit/offset (MCP-07), only that slice of the
            children is serialized and the result gains "total"
            and "next_offset"; with neither, the result is exactly
            the pre-paging one.
        """
        try:
            # Access the application's browser instance instead of creating a new one
            app = self.application()
            if not app:
                raise RuntimeError("Could not access Live application")
                
            # Check if browser is available
            if not hasattr(app, 'browser') or app.browser is None:
                raise RuntimeError("Browser is not available in the Live application")

            # MCP-07: a repeated identical read (same path and
            # page) is served from the read cache without
            # re-navigating the browser; the cache is invalidated
            # on load and on refresh_browser_cache.
            _, read_cache = self._browser_cache_state()
            # SI-237: refuse an unhashable path by name before it
            # can crash the cache lookup as a raw TypeError (the
            # key below embeds the raw path in both its forms).
            _require_hashable_cache_key(path, "path")
            # SI-75: coerce the paging values BEFORE building the
            # cache key. Keying on the raw values crashed the lookup
            # on unhashable shapes (limit=[1] -> "unhashable type:
            # 'list'") before this handler's lenient coercion could see
            # them, and keyed the same coerced page under different
            # entries for different raw forms (5 vs "5"). The
            # unpaged form (both absent) keeps its exact key and
            # result shape, so it never shares an entry with a
            # paged read.
            if limit is None and offset is None:
                page_limit = None
                page_offset = None
                cache_key = ("items", path, limit, offset)
            else:
                page_offset = _coerce_page_int(offset, 0)
                page_limit = _coerce_page_int(
                    limit, DEFAULT_BROWSER_ITEMS_PAGE_SIZE,
                    MAX_BROWSER_ITEMS_PAGE_SIZE, minimum=1)
                cache_key = ("items", path, page_limit, page_offset)
            cached = self._browser_cache_get(read_cache, cache_key)
            if cached is not None:
                return cached

            # The attribute list drives root resolution below
            # (and the unknown-category result). It used to also
            # be logged on every call - a full dir() dump per
            # read (MCP-07 logging diet).
            browser_attrs = [attr for attr in dir(app.browser) if not attr.startswith('_')]
                
            # Parse the path
            path_parts = path.split("/")
            if not path_parts:
                raise ValueError("Invalid path")
            
            # Determine the root category
            root_category = path_parts[0].lower()
            current_item = None
            
            # Check standard categories first
            if root_category == "instruments" and hasattr(app.browser, 'instruments'):
                current_item = app.browser.instruments
            elif root_category == "sounds" and hasattr(app.browser, 'sounds'):
                current_item = app.browser.sounds
            elif root_category == "drums" and hasattr(app.browser, 'drums'):
                current_item = app.browser.drums
            elif root_category == "audio_effects" and hasattr(app.browser, 'audio_effects'):
                current_item = app.browser.audio_effects
            elif root_category == "midi_effects" and hasattr(app.browser, 'midi_effects'):
                current_item = app.browser.midi_effects
            else:
                # Try to find the category in other browser attributes
                found = False
                for attr in browser_attrs:
                    if attr.lower() == root_category:
                        try:
                            current_item = getattr(app.browser, attr)
                            found = True
                            break
                        except Exception as e:
                            self.log_message("Error accessing browser attribute {0}: {1}".format(attr, str(e)))
                
                if not found:
                    # If we still haven't found the category, return available categories
                    return {
                        "path": path,
                        "error": "Unknown or unavailable category: {0}".format(root_category),
                        "available_categories": browser_attrs,
                        "items": []
                    }
            
            # Navigate through the path
            for i in range(1, len(path_parts)):
                part = path_parts[i]
                if not part:  # Skip empty parts
                    continue
                
                if not hasattr(current_item, 'children'):
                    return {
                        "path": path,
                        "error": "Item at '{0}' has no children".format('/'.join(path_parts[:i])),
                        "items": []
                    }
                
                found = False
                for child in current_item.children:
                    if hasattr(child, 'name') and child.name.lower() == part.lower():
                        current_item = child
                        found = True
                        break
                
                if not found:
                    return {
                        "path": path,
                        "error": "Path part '{0}' not found".format(part),
                        "items": []
                    }
            
            # Get items at the current path
            children = []
            if hasattr(current_item, 'children'):
                children = list(current_item.children)

            # MCP-07: paging is opt-in - with no limit/offset every
            # child is serialized, exactly as before; with either,
            # only the requested slice is and the result reports the
            # full count and where the next page starts.
            if limit is None and offset is None:
                page = children
            else:
                page = children[page_offset:page_offset + page_limit]

            items = []
            for child in page:
                item_info = {
                    "name": child.name if hasattr(child, 'name') else "Unknown",
                    "is_folder": hasattr(child, 'children') and bool(child.children),
                    "is_device": hasattr(child, 'is_device') and child.is_device,
                    "is_loadable": hasattr(child, 'is_loadable') and child.is_loadable,
                    "uri": child.uri if hasattr(child, 'uri') else None
                }
                items.append(item_info)

            result = {
                "path": path,
                "name": current_item.name if hasattr(current_item, 'name') else "Unknown",
                "uri": current_item.uri if hasattr(current_item, 'uri') else None,
                "is_folder": hasattr(current_item, 'children') and bool(current_item.children),
                "is_device": hasattr(current_item, 'is_device') and current_item.is_device,
                "is_loadable": hasattr(current_item, 'is_loadable') and current_item.is_loadable,
                "items": items
            }
            if limit is not None or offset is not None:
                next_offset = page_offset + len(page)
                result["total"] = len(children)
                result["next_offset"] = (
                    next_offset if next_offset < len(children) else None)

            self.log_message("Retrieved {0} items at path: {1}".format(len(items), path))
            # MCP-07: only successful reads are cached - the error
            # results above return early and are never stored.
            self._browser_cache_put(read_cache, cache_key, result,
                                    MAX_BROWSER_READ_CACHE_ENTRIES)
            return result
            
        except Exception as e:
            self.log_message("Error getting browser items at path: {0}".format(str(e)))
            self.log_message(traceback.format_exc())
            raise
