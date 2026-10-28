"""HMCP frame codec: partial, coalesced, truncated and hostile input."""

from __future__ import annotations

import random

import pytest

from hatari_mcp.protocol.errors import ProtocolError
from hatari_mcp.protocol.frame import (
    HEADER_SIZE,
    Frame,
    FrameDecoder,
    FrameEncoder,
    FrameType,
    Flags,
)


@pytest.mark.parametrize(
    "frame_type",
    [
        FrameType.HELLO,
        FrameType.HELLO_ACK,
        FrameType.REQUEST,
        FrameType.OK,
        FrameType.ERR,
        FrameType.NOTIFY,
        FrameType.PING,
        FrameType.PONG,
    ],
)
def test_round_trip_all_types(frame_type: FrameType) -> None:
    request_id = 0 if frame_type in {FrameType.HELLO, FrameType.NOTIFY, FrameType.PING, FrameType.PONG} else 7
    frame = Frame(frame_type, request_id, b"payload", flags=int(Flags.BINARY))
    encoded = FrameEncoder().encode(frame)
    assert len(encoded) == HEADER_SIZE + len(frame.payload)
    decoded = FrameDecoder().feed(encoded)
    assert decoded == [frame]


def test_partial_header_and_payload() -> None:
    encoded = Frame(FrameType.REQUEST, 1, b"hello world").encode()
    decoder = FrameDecoder()
    frames: list[Frame] = []
    for byte in encoded:
        frames.extend(decoder.feed(bytes([byte])))
    assert frames == [Frame(FrameType.REQUEST, 1, b"hello world")]


def test_coalesced_frames() -> None:
    frames = [Frame(FrameType.REQUEST, index, bytes([index])) for index in range(1, 5)]
    decoder = FrameDecoder()
    assert decoder.feed(b"".join(frame.encode() for frame in frames)) == frames


def test_eof_at_boundary_is_clean_and_partial_is_truncated() -> None:
    decoder = FrameDecoder()
    decoder.feed(Frame(FrameType.PING, 0, b"x").encode())
    decoder.eof()
    assert decoder.closed is True
    with pytest.raises(ProtocolError):
        decoder.feed(b"")

    partial = FrameDecoder()
    partial.feed(Frame(FrameType.PONG, 0, b"xxx").encode()[:-1])
    with pytest.raises(ProtocolError) as excinfo:
        partial.eof()
    assert excinfo.value.code == "truncated_frame"


def test_oversized_declared_length_is_rejected_before_payload() -> None:
    header = Frame(FrameType.REQUEST, 1, b"").encode()[:12]
    # Re-pack the length field only; no payload follows.
    import struct

    hostile = header + struct.pack("!I", 9 * 1024 * 1024)
    with pytest.raises(ProtocolError) as excinfo:
        FrameDecoder(max_payload=8 * 1024 * 1024).feed(hostile)
    assert excinfo.value.code == "frame_too_large"


def test_bad_magic_version_type_flags_and_reserved() -> None:
    good = bytearray(Frame(FrameType.REQUEST, 1, b"x").encode())
    cases = []
    bad_magic = bytearray(good)
    bad_magic[:4] = b"XXXX"
    cases.append((bad_magic, "bad_frame"))
    bad_version = bytearray(good)
    bad_version[4] = 9
    cases.append((bad_version, "bad_frame"))
    bad_type = bytearray(good)
    bad_type[5] = 99
    cases.append((bad_type, "bad_frame"))
    bad_flags = bytearray(good)
    bad_flags[6] = 0x80
    cases.append((bad_flags, "bad_frame"))
    reserved = bytearray(good)
    reserved[7] = 1
    cases.append((reserved, "bad_frame"))
    for data, code in cases:
        with pytest.raises(ProtocolError) as excinfo:
            FrameDecoder().feed(bytes(data))
        assert excinfo.value.code == code


def test_request_frames_require_nonzero_id() -> None:
    with pytest.raises(ProtocolError):
        Frame(FrameType.REQUEST, 0, b"x").encode()
    with pytest.raises(ProtocolError):
        Frame(FrameType.OK, 0, b"x").encode()


def test_fuzzed_input_never_hangs_or_returns_partial_frames() -> None:
    rng = random.Random(1234)
    for _ in range(400):
        data = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 96)))
        decoder = FrameDecoder(max_payload=1024)
        try:
            decoder.feed(data)
        except ProtocolError:
            pass
        assert decoder.buffered_bytes <= max(HEADER_SIZE, 1024 + HEADER_SIZE) or decoder.buffered_bytes == 0


def test_random_valid_frames_split_at_random_boundaries() -> None:
    rng = random.Random(99)
    frames = [
        Frame(FrameType.REQUEST, index, bytes(rng.randrange(256) for _ in range(rng.randrange(0, 40))))
        for index in range(1, 30)
    ]
    stream = b"".join(frame.encode() for frame in frames)
    decoder = FrameDecoder()
    decoded: list[Frame] = []
    offset = 0
    while offset < len(stream):
        step = rng.randrange(1, 23)
        decoded.extend(decoder.feed(stream[offset : offset + step]))
        offset += step
    assert decoded == frames


def test_decoder_rejects_zero_request_id_before_payload_and_closes_on_eof() -> None:
    import struct

    # REQUEST with id 0 and a declared payload that has not arrived yet.
    header = struct.pack("!4sBBBBII", b"HMCP", 1, int(FrameType.REQUEST), 0, 0, 0, 1024)
    with pytest.raises(ProtocolError):
        FrameDecoder().feed(header)

    decoder = FrameDecoder()
    decoder.feed(Frame(FrameType.PONG, 0, b"x").encode()[:-1])
    with pytest.raises(ProtocolError):
        decoder.eof()
    assert decoder.closed is True
    with pytest.raises(ProtocolError):
        decoder.feed(b"")
