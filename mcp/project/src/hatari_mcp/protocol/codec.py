"""JSON control and binary metadata codecs for HMCP v1 payloads."""

from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass
from typing import Any

from .errors import BAD_REQUEST, PROTOCOL, TOO_LARGE, make_protocol_error

MAX_META_LEN = 1024 * 1024
MAX_BINARY_LEN = 64 * 1024 * 1024
META_HEADER = struct.Struct("!I")

VALID_SPACES = frozenset({"cpu", "dsp", "ram", "fb"})
VALID_ENDIAN = frozenset({"big", "little"})
DEVICE_MAX = 0xFFFFFFFF


def _json_dumps(value: Any) -> str:
    try:
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise make_protocol_error(BAD_REQUEST, f"Value is not JSON serialisable: {exc}") from exc


def _json_loads(data: bytes | str) -> Any:
    try:
        text = data.decode("utf-8") if isinstance(data, bytes) else data
        return json.loads(
            text,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise make_protocol_error(PROTOCOL, f"Malformed JSON payload: {exc}") from exc


def encode_control(op: str, params: dict[str, Any] | None = None) -> bytes:
    if not isinstance(op, str) or not op or any(char in op for char in "\x00\r\n"):
        raise make_protocol_error(BAD_REQUEST, "Control op must be a non-empty single-line string.")
    if params is not None and not isinstance(params, dict):
        raise make_protocol_error(BAD_REQUEST, "Control params must be a JSON object.")
    message: dict[str, Any] = {"op": op}
    if params:
        message["params"] = params
    return _json_dumps(message).encode("utf-8")


def decode_control(payload: bytes) -> dict[str, Any]:
    value = _json_loads(payload)
    if not isinstance(value, dict):
        raise make_protocol_error(PROTOCOL, "Control payload must be a JSON object.")
    op = value.get("op")
    if not isinstance(op, str) or not op:
        raise make_protocol_error(PROTOCOL, "Control payload has no string 'op'.")
    params = value.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise make_protocol_error(PROTOCOL, "Control 'params' must be an object.")
    return {"op": op, "params": params}


def encode_ok(result: Any = None, *, extra: dict[str, Any] | None = None) -> bytes:
    value: dict[str, Any] = {"ok": True, "result": result}
    if extra:
        value.update(extra)
    return _json_dumps(value).encode("utf-8")


def encode_error(
    code: str,
    message: str,
    hint: str = "",
    *,
    data: Any = None,
) -> bytes:
    err: dict[str, Any] = {"code": code, "message": message}
    if hint:
        err["hint"] = hint
    if data is not None:
        err["data"] = data
    return _json_dumps({"ok": False, "error": err}).encode("utf-8")


def decode_response(payload: bytes) -> tuple[bool, Any]:
    """Decode ``{"ok": true, "result": ...}`` or ``{"ok": false, "error": ...}``.

    Returns ``(True, result)`` or ``(False, error_dict)``. The caller decides
    whether to turn the error dict into a ProtocolError.
    """
    value = _json_loads(payload)
    if not isinstance(value, dict) or not isinstance(value.get("ok"), bool):
        raise make_protocol_error(PROTOCOL, "Response payload must contain a boolean 'ok'.")
    if value["ok"]:
        return True, value.get("result")
    err = value.get("error")
    if not isinstance(err, dict):
        raise make_protocol_error(PROTOCOL, "Error response has no error object.")
    return False, err


# -- binary ---------------------------------------------------------------


@dataclass(frozen=True)
class BinaryResult:
    meta: dict[str, Any]
    data: bytes

    def as_dict(self) -> dict[str, Any]:
        return {
            "meta": dict(self.meta),
            "data": self.data,
            "length": len(self.data),
            "sha256": hashlib.sha256(self.data).hexdigest(),
        }


def _require_int(meta: dict[str, Any], key: str, *, minimum: int = 0, maximum: int | None = None) -> None:
    if key not in meta:
        return
    value = meta[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise make_protocol_error(PROTOCOL, f"Binary meta {key!r} must be an integer.")
    if value < minimum or (maximum is not None and value > maximum):
        bounds = f">= {minimum}" if maximum is None else f"{minimum}..{maximum}"
        raise make_protocol_error(PROTOCOL, f"Binary meta {key!r} must be {bounds}.")


def validate_binary_meta(meta: Any, data_length: int) -> dict[str, Any]:
    if not isinstance(meta, dict):
        raise make_protocol_error(PROTOCOL, "Binary metadata must be a JSON object.")
    space = meta.get("space")
    if space is not None and space not in VALID_SPACES:
        raise make_protocol_error(
            PROTOCOL,
            f"Unknown binary space {space!r}.",
            "Use cpu, dsp, ram or fb.",
        )
    for key in ("address", "offset", "length", "chunk_length", "total_length", "width", "height", "stride", "bpp", "vbl", "frame", "total", "sequence"):
        maximum = 64 * 1024 * 1024 if key in {"length", "chunk_length", "total_length"} else DEVICE_MAX
        _require_int(meta, key, maximum=maximum)
    if "length" in meta and meta["length"] != data_length:
        if not ("total" in meta or "sequence" in meta):
            raise make_protocol_error(
                PROTOCOL,
                f"Binary meta length {meta['length']} does not match {data_length} bytes.",
            )
    if "chunk_length" in meta and meta["chunk_length"] != data_length:
        raise make_protocol_error(
            PROTOCOL,
            f"Binary meta chunk_length {meta['chunk_length']} does not match {data_length} bytes.",
        )
    if "total_length" in meta and meta["total_length"] < data_length:
        raise make_protocol_error(PROTOCOL, "Binary meta total_length is smaller than a chunk.")
    if "total" in meta or "sequence" in meta:
        total = meta.get("total")
        sequence = meta.get("sequence")
        if not isinstance(total, int) or not isinstance(sequence, int):
            raise make_protocol_error(PROTOCOL, "Chunked binary metadata needs integer total and sequence.")
        if total <= 0 or sequence < 0 or sequence >= total:
            raise make_protocol_error(PROTOCOL, "Chunk sequence/total are out of range.")
    if "endian" in meta and meta["endian"] not in VALID_ENDIAN:
        raise make_protocol_error(PROTOCOL, f"Unknown endian value {meta['endian']!r}.")
    if "palette" in meta and meta["palette"] is not None:
        palette = meta["palette"]
        if not isinstance(palette, list):
            raise make_protocol_error(PROTOCOL, "Binary meta palette must be a list.")
        for entry in palette:
            if (
                not isinstance(entry, (list, tuple))
                or len(entry) not in (3, 4)
                or not all(isinstance(part, int) and 0 <= part <= 255 for part in entry)
            ):
                raise make_protocol_error(PROTOCOL, "Palette entries must be 3 or 4 bytes in 0..255.")
    if "sha256" in meta and meta["sha256"] is not None:
        digest = meta["sha256"]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdefABCDEF" for char in digest)
        ):
            raise make_protocol_error(PROTOCOL, "Binary meta sha256 must be 64 hex characters.")
    return dict(meta)


def encode_binary(meta: dict[str, Any] | None, data: bytes) -> bytes:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise make_protocol_error(BAD_REQUEST, "Binary data must be bytes.")
    raw = bytes(data)
    if len(raw) > MAX_BINARY_LEN:
        raise make_protocol_error(TOO_LARGE, f"Binary payload exceeds {MAX_BINARY_LEN} bytes.")
    value: dict[str, Any] = dict(meta or {})
    value.setdefault("length", len(raw))
    if value["length"] != len(raw) and "total" not in value and "sequence" not in value:
        raise make_protocol_error(PROTOCOL, "Binary meta length does not match data.")
    value.setdefault("sha256", hashlib.sha256(raw).hexdigest())
    validate_binary_meta(value, len(raw))
    encoded_meta = _json_dumps(value).encode("utf-8")
    if len(encoded_meta) > MAX_META_LEN:
        raise make_protocol_error(TOO_LARGE, f"Binary metadata exceeds {MAX_META_LEN} bytes.")
    return META_HEADER.pack(len(encoded_meta)) + encoded_meta + raw


def decode_binary(payload: bytes) -> BinaryResult:
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise make_protocol_error(PROTOCOL, "Binary payload must be bytes.")
    raw = bytes(payload)
    if len(raw) < META_HEADER.size:
        raise make_protocol_error(PROTOCOL, "Binary payload is shorter than its metadata header.")
    (meta_len,) = META_HEADER.unpack_from(raw, 0)
    if meta_len > MAX_META_LEN:
        raise make_protocol_error(TOO_LARGE, "Binary metadata length exceeds the limit.")
    if meta_len > len(raw) - META_HEADER.size:
        raise make_protocol_error(PROTOCOL, "Binary metadata length runs past the payload.")
    meta_bytes = raw[META_HEADER.size : META_HEADER.size + meta_len]
    data = raw[META_HEADER.size + meta_len :]
    if len(data) > MAX_BINARY_LEN:
        raise make_protocol_error(TOO_LARGE, "Binary data exceeds the limit.")
    value = _json_loads(meta_bytes)
    meta = validate_binary_meta(value, len(data))
    digest = meta.get("sha256")
    if digest is not None:
        if hashlib.sha256(data).hexdigest().lower() != digest.lower():
            raise make_protocol_error(PROTOCOL, "Binary payload sha256 does not match the metadata.")
    return BinaryResult(meta, data)
