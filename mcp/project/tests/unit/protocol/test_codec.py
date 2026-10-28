"""JSON control and binary metadata codec validation."""

from __future__ import annotations

import hashlib
import struct

import pytest

from hatari_mcp.protocol import codec
from hatari_mcp.protocol.errors import ProtocolError


def test_control_round_trip() -> None:
    payload = codec.encode_control("mem.read", {"address": 0x1000, "length": 4})
    assert codec.decode_control(payload) == {
        "op": "mem.read",
        "params": {"address": 0x1000, "length": 4},
    }


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"[]",
        b'{"op": ""}',
        b'{"op": 1}',
        b'{"op": "x", "params": []}',
        b'{"op": "x", "params": {"v": NaN}}',
    ],
)
def test_control_rejects_malformed_payloads(payload: bytes) -> None:
    with pytest.raises(ProtocolError):
        codec.decode_control(payload)


def test_control_rejects_bad_op_and_params() -> None:
    for op, params in [("", None), ("a\nb", None), ("x", [])]:
        with pytest.raises(ProtocolError):
            codec.encode_control(op, params)  # type: ignore[arg-type]


def test_ok_and_error_responses() -> None:
    ok, value = codec.decode_response(codec.encode_ok({"value": 4}))
    assert ok is True and value == {"value": 4}
    ok, err = codec.decode_response(codec.encode_error("out_of_range", "bad", "hint", data={"x": 1}))
    assert ok is False
    assert err == {"code": "out_of_range", "message": "bad", "hint": "hint", "data": {"x": 1}}
    with pytest.raises(ProtocolError):
        codec.decode_response(b'{"ok": "yes"}')
    with pytest.raises(ProtocolError):
        codec.decode_response(b'{"ok": false}')


def test_binary_round_trip_and_automatic_digest() -> None:
    payload = codec.encode_binary({"space": "ram", "address": 0x1000}, b"abcd")
    result = codec.decode_binary(payload)
    assert result.meta["space"] == "ram"
    assert result.meta["address"] == 0x1000
    assert result.meta["length"] == 4
    assert result.data == b"abcd"
    assert result.meta["sha256"] == hashlib.sha256(b"abcd").hexdigest()


def test_binary_digest_mismatch_is_rejected() -> None:
    payload = bytearray(codec.encode_binary({"space": "cpu"}, b"abcd"))
    # Flip the last data byte; the digest no longer matches.
    payload[-1] ^= 0xFF
    with pytest.raises(ProtocolError):
        codec.decode_binary(bytes(payload))


def test_binary_length_mismatch_is_rejected() -> None:
    meta = {"space": "ram", "length": 9}
    with pytest.raises(ProtocolError):
        codec.encode_binary(meta, b"abcd")


def test_binary_short_and_bad_metadata_lengths() -> None:
    with pytest.raises(ProtocolError):
        codec.decode_binary(b"\x00\x00")
    payload = struct.pack("!I", 100) + b"{}"
    with pytest.raises(ProtocolError):
        codec.decode_binary(payload)
    payload = struct.pack("!I", 2 * 1024 * 1024) + b"x"
    with pytest.raises(ProtocolError):
        codec.decode_binary(payload)


@pytest.mark.parametrize(
    "meta",
    [
        {"space": "host"},
        {"space": "ram", "address": -1},
        {"space": "ram", "width": "wide"},
        {"space": "ram", "endian": "middle"},
        {"space": "ram", "palette": [[1, 2]]},
        {"space": "ram", "palette": [[1, 2, 999]]},
        {"space": "ram", "sequence": 2, "total": 2},
    ],
)
def test_binary_rejects_bad_metadata(meta: dict) -> None:
    with pytest.raises(ProtocolError):
        codec.encode_binary(meta, b"x")


def test_binary_chunk_metadata_is_accepted() -> None:
    meta = {"space": "ram", "sequence": 1, "total": 3, "total_length": 9, "length": 3}
    result = codec.decode_binary(codec.encode_binary(meta, b"abc"))
    assert result.meta["sequence"] == 1
    assert result.data == b"abc"
