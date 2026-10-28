"""HMCP v1 frame header, encoder and streaming decoder.

Wire layout (all integers network byte order):

======  ====  ====================================================
Offset  Size  Field
======  ====  ====================================================
0       4     magic ``HMCP``
4       1     protocol version (1)
5       1     frame type
6       1     flags (``BINARY``, ``MORE``, ``NO_REPLY``)
7       1     reserved, must be zero
8       4     request id (nonzero for request/response frames)
12      4     payload length
16      n     payload
======  ====  ====================================================

The decoder validates the header before allocating or waiting for a payload.
It never buffers more than one declared frame beyond what the caller feeds and
rejects an oversized length immediately.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum, IntFlag

from .errors import BAD_FRAME, TOO_LARGE, TRUNCATED, make_protocol_error

MAGIC = b"HMCP"
PROTOCOL_VERSION = 1
HEADER_SIZE = 16
HEADER = struct.Struct("!4sBBBBII")

DEFAULT_MAX_PAYLOAD = 8 * 1024 * 1024
ABSOLUTE_MAX_PAYLOAD = 64 * 1024 * 1024

MAX_REQUEST_ID = 0xFFFFFFFF
KNOWN_FLAG_BITS = 0x07


class FrameType(IntEnum):
    HELLO = 1
    HELLO_ACK = 2
    REQUEST = 3
    OK = 4
    ERR = 5
    NOTIFY = 6
    PING = 7
    PONG = 8


class Flags(IntFlag):
    BINARY = 0x01
    MORE = 0x02
    NO_REPLY = 0x04


REQUEST_TYPES = frozenset({FrameType.REQUEST, FrameType.OK, FrameType.ERR})
RESPONSE_TYPES = frozenset({FrameType.OK, FrameType.ERR, FrameType.HELLO_ACK, FrameType.PONG})


@dataclass(frozen=True)
class Frame:
    type: FrameType
    request_id: int = 0
    payload: bytes = b""
    version: int = PROTOCOL_VERSION
    flags: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", bytes(self.payload))

    def encode(self, *, max_payload: int = DEFAULT_MAX_PAYLOAD) -> bytes:
        validate_frame(self, max_payload=max_payload)
        return HEADER.pack(
            MAGIC,
            self.version,
            int(self.type),
            int(self.flags),
            0,
            self.request_id,
            len(self.payload),
        ) + self.payload


class FrameEncoder:
    """Small helper for callers that emit more than one frame."""

    def __init__(self, *, max_payload: int = DEFAULT_MAX_PAYLOAD):
        self.max_payload = _checked_max(max_payload)

    def encode(self, frame: Frame) -> bytes:
        return frame.encode(max_payload=self.max_payload)

    def encode_many(self, frames: list[Frame]) -> bytes:
        return b"".join(self.encode(frame) for frame in frames)


def _checked_max(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise make_protocol_error(BAD_FRAME, "Max payload must be an integer.")
    if value < 0 or value > ABSOLUTE_MAX_PAYLOAD:
        raise make_protocol_error(
            BAD_FRAME,
            f"Max payload must be between 0 and {ABSOLUTE_MAX_PAYLOAD}.",
        )
    return value


def _checked_type(value: object) -> FrameType:
    try:
        return FrameType(int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise make_protocol_error(BAD_FRAME, f"Unknown frame type: {value!r}") from None


def validate_frame(frame: Frame, *, max_payload: int = DEFAULT_MAX_PAYLOAD) -> None:
    max_payload = _checked_max(max_payload)
    if frame.version != PROTOCOL_VERSION:
        raise make_protocol_error(
            BAD_FRAME,
            f"Unsupported protocol version {frame.version}; expected {PROTOCOL_VERSION}.",
        )
    frame_type = _checked_type(frame.type)
    request_id = frame.request_id
    if not isinstance(request_id, int) or isinstance(request_id, bool):
        raise make_protocol_error(BAD_FRAME, "Request id must be an integer.")
    if request_id < 0 or request_id > MAX_REQUEST_ID:
        raise make_protocol_error(BAD_FRAME, "Request id is out of range.")
    if frame_type in REQUEST_TYPES and request_id == 0:
        raise make_protocol_error(BAD_FRAME, f"{frame_type.name} requires a nonzero request id.")
    flags = frame.flags
    if not isinstance(flags, int) or isinstance(flags, bool):
        raise make_protocol_error(BAD_FRAME, "Flags must be an integer.")
    if flags & ~KNOWN_FLAG_BITS:
        raise make_protocol_error(BAD_FRAME, f"Unknown frame flags: 0x{flags:x}.")
    if (flags & Flags.MORE) and not (flags & Flags.BINARY):
        raise make_protocol_error(BAD_FRAME, "MORE flag requires BINARY.")
    if not isinstance(frame.payload, (bytes, bytearray, memoryview)):
        raise make_protocol_error(BAD_FRAME, "Payload must be bytes.")
    if len(frame.payload) > max_payload:
        raise make_protocol_error(
            TOO_LARGE,
            f"Payload is {len(frame.payload)} bytes, limit is {max_payload}.",
            "Send smaller chunks or negotiate a larger limit.",
        )


class FrameDecoder:
    """Incremental decoder for partial, coalesced and truncated input."""

    def __init__(self, *, max_payload: int = DEFAULT_MAX_PAYLOAD):
        self.max_payload = _checked_max(max_payload)
        self._buffer = bytearray()
        self._closed = False

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    @property
    def closed(self) -> bool:
        return self._closed

    def feed(self, data: bytes | bytearray | memoryview) -> list[Frame]:
        if self._closed:
            raise make_protocol_error(BAD_FRAME, "Decoder is closed after EOF.")
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise make_protocol_error(BAD_FRAME, "Decoder input must be bytes.")
        if data:
            self._buffer.extend(data)

        frames: list[Frame] = []
        while True:
            if len(self._buffer) < HEADER_SIZE:
                break
            magic, version, type_byte, flags, reserved, request_id, length = HEADER.unpack_from(
                self._buffer, 0
            )
            if magic != MAGIC:
                raise make_protocol_error(
                    BAD_FRAME,
                    f"Bad frame magic: {magic!r}.",
                    "This stream is not HMCP or the connection is desynchronised.",
                )
            if version != PROTOCOL_VERSION:
                raise make_protocol_error(
                    BAD_FRAME,
                    f"Unsupported protocol version {version}; expected {PROTOCOL_VERSION}.",
                )
            if reserved != 0:
                raise make_protocol_error(BAD_FRAME, "Reserved header byte must be zero.")
            if length > self.max_payload:
                raise make_protocol_error(
                    TOO_LARGE,
                    f"Declared payload length {length} exceeds limit {self.max_payload}.",
                    "The stream is rejected before allocating the payload.",
                )
            try:
                frame_type = FrameType(type_byte)
            except ValueError:
                raise make_protocol_error(BAD_FRAME, f"Unknown frame type: {type_byte}.") from None
            if flags & ~KNOWN_FLAG_BITS:
                raise make_protocol_error(BAD_FRAME, f"Unknown frame flags: 0x{flags:x}.")
            if (flags & Flags.MORE) and not (flags & Flags.BINARY):
                raise make_protocol_error(BAD_FRAME, "MORE flag requires BINARY.")
            if frame_type in REQUEST_TYPES and request_id == 0:
                raise make_protocol_error(
                    BAD_FRAME, f"{frame_type.name} requires a nonzero request id."
                )
            total = HEADER_SIZE + length
            if len(self._buffer) < total:
                break
            payload = bytes(self._buffer[HEADER_SIZE:total])
            del self._buffer[:total]
            frame = Frame(
                type=frame_type,
                request_id=request_id,
                payload=payload,
                version=version,
                flags=flags,
            )
            validate_frame(frame, max_payload=self.max_payload)
            frames.append(frame)
        return frames

    def eof(self) -> None:
        """Mark EOF; a partial frame at EOF is a truncated-frame error."""
        if self._buffer:
            remaining = len(self._buffer)
            self._buffer.clear()
            self._closed = True
            raise make_protocol_error(
                TRUNCATED,
                f"Stream ended with {remaining} bytes of an incomplete frame.",
                "The peer closed the connection mid-frame.",
            )
        self._closed = True

    def reset(self) -> None:
        self._buffer.clear()
        self._closed = False
