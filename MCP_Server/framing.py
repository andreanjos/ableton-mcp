"""Framing v2 for the AbletonMCP wire protocol (MCP-03).

Protocol v1 sends each request and response as one raw JSON object and
infers the message boundary by re-parsing the accumulated buffer after
every 8192-byte chunk. That is quadratic in the number of chunks: the
lane audit measured ~61x the one-shot parse cost at 1 MiB and ~133x at
2 MiB, on both endpoints.

Framing v2 replaces boundary inference with an explicit prefix:

    +-------------------------------+============================+
    | payload length N (4 bytes,    | JSON payload (N bytes,     |
    | unsigned big-endian)          | UTF-8)                     |
    +-------------------------------+============================+

N counts payload bytes only and must satisfy 2 <= N <= MAX_FRAME_BYTES
(a JSON object is at least "{}"; the ceiling is the same 16 MiB the v1
Remote Script already applies to a whole request). The payload is the
same JSON object v1 would have sent; in v2 it always carries a native
``request_id`` (allocated by the sender when the caller did not supply
one) and the response echoes it - correlation is the MCP-01 mechanism,
not a second one layered on top.

Framing is negotiated, never assumed: the Remote Script advertises
``agent0_framing: ["v1", "v2"]`` in its handshake, sniffs the framing
of each message at its boundary, and always answers in the framing
the request used - so a connection opens with a v1 handshake and can
switch to framed exchanges from the next message on. A v1 peer
talking to a v2 endpoint sees byte-identical v1 behaviour.

This module is the MCP-server side of the codec. The Remote Script
cannot import the package, so it carries a miniature implementation of
the same format (_FrameDecoder / _encode_frame in its __init__.py);
the two must never drift apart in what they accept.
"""

from __future__ import annotations

import struct

#: Frame payload ceiling: the v1 request ceiling, reused so the size
#: limit is identical in both framings, boundary included: a payload
#: of exactly this size is legal in v2, as a whole request of this
#: size is in v1.
MAX_FRAME_BYTES = 16 * 1024 * 1024

#: Smallest legal payload: "{}".
MIN_FRAME_BYTES = 2

_HEADER = struct.Struct(">I")


class FrameError(Exception):
    """A peer sent bytes that cannot be framed v2.

    The stream position is unknowable once a length prefix is invalid,
    so a FrameError always means the connection must be dropped -
    never retried in-band.
    """


def encode_frame(payload: bytes) -> bytes:
    """Prefix *payload* with its 4-byte big-endian length."""
    if not MIN_FRAME_BYTES <= len(payload) <= MAX_FRAME_BYTES:
        raise FrameError(
            "Cannot frame a %d-byte payload (legal range is %d..%d bytes)"
            % (len(payload), MIN_FRAME_BYTES, MAX_FRAME_BYTES))
    return _HEADER.pack(len(payload)) + payload


class FrameDecoder:
    """Incremental decoder: feed chunks, pop complete frame payloads.

    Each payload is extracted exactly once and consumed bytes are
    removed from the front of the buffer, so the total work is linear
    in the bytes received - the property v1's re-parse loop lacks.
    """

    def __init__(self, max_frame_bytes: int = MAX_FRAME_BYTES):
        self._buffer = bytearray()
        self._max_frame_bytes = max_frame_bytes

    def feed(self, data: bytes) -> None:
        self._buffer += data

    @property
    def pending_bytes(self) -> int:
        """Bytes buffered toward an incomplete frame."""
        return len(self._buffer)

    def next_frame(self) -> bytes | None:
        """Pop one complete frame payload, or None if more bytes are
        needed. Raises FrameError on a declared length outside the
        legal range."""
        if len(self._buffer) < _HEADER.size:
            return None
        (length,) = _HEADER.unpack(bytes(self._buffer[:_HEADER.size]))
        if not MIN_FRAME_BYTES <= length <= self._max_frame_bytes:
            raise FrameError(
                "Invalid frame length %d (legal range is %d..%d bytes)"
                % (length, MIN_FRAME_BYTES, self._max_frame_bytes))
        if len(self._buffer) < _HEADER.size + length:
            return None
        payload = bytes(self._buffer[_HEADER.size:_HEADER.size + length])
        del self._buffer[:_HEADER.size + length]
        return payload
