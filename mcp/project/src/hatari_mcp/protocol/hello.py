"""Startup banner and HELLO/HELLO_ACK negotiation for HMCP v1."""

from __future__ import annotations

import hmac
import re
from dataclasses import dataclass
from typing import Any, Iterable

from . import codec
from .capabilities import CANONICAL, normalize_reported
from .errors import BAD_REQUEST, PROTOCOL, make_protocol_error
from .frame import PROTOCOL_VERSION

BANNER_PREFIX = "HATARI-MCP-PROTOCOL"
BANNER_NONCE_ENV = "HATARI_MCP_PROTOCOL_NONCE"
BANNER_NONCE_PARAM = "nonce"
BANNER_RE = re.compile(
    r"^\s*HATARI-MCP-PROTOCOL\s+(?P<version>\d+)"
    r"(?:\s+capabilities=(?P<caps>[^\s]+))?"
    r"(?:\s+nonce=(?P<nonce>[^\s]+))?\s*$"
)


@dataclass(frozen=True)
class Banner:
    version: int
    capabilities: frozenset[str]
    unknown_capabilities: tuple[str, ...] = ()
    raw: str = ""


@dataclass(frozen=True)
class HelloAck:
    version: int
    capabilities: frozenset[str]
    unknown_capabilities: tuple[str, ...]
    hatari_version: str | None = None
    hatari_commit: str | None = None
    build_flags: str | None = None
    limits: dict[str, Any] | None = None


def parse_capability_list(value: object) -> tuple[set[str], list[str]]:
    return normalize_reported(value)


def parse_banner_line(line: str, *, expected_nonce: str | None = None) -> Banner | None:
    """Parse one startup banner.

    Only HMCP v1 is accepted. When ``expected_nonce`` is supplied the banner
    must carry the same out-of-band per-launch nonce; this prevents guest
    stderr output from forging the banner. The banner is advisory until a
    HELLO/HELLO_ACK handshake completes.
    """
    if not isinstance(line, str):
        return None
    match = BANNER_RE.match(line)
    if not match:
        return None
    version = int(match.group("version"))
    if version != PROTOCOL_VERSION:
        raise make_protocol_error(
            PROTOCOL,
            f"Unsupported protocol banner version {version}; expected {PROTOCOL_VERSION}.",
        )
    if expected_nonce is not None:
        supplied = match.group("nonce")
        if not supplied or not hmac.compare_digest(str(supplied), str(expected_nonce)):
            raise make_protocol_error(PROTOCOL, "Protocol banner nonce is missing or does not match.")
    caps, unknown = parse_capability_list(match.group("caps") or "")
    return Banner(version, frozenset(caps), tuple(unknown), line.strip())


def find_banner(
    text_or_lines: str | Iterable[str], *, expected_nonce: str | None = None
) -> Banner | None:
    if isinstance(text_or_lines, str):
        lines: Iterable[str] = text_or_lines.splitlines()
    else:
        lines = text_or_lines
    for line in lines:
        if BANNER_PREFIX not in line:
            continue
        banner = parse_banner_line(line, expected_nonce=expected_nonce)
        if banner is not None:
            return banner
    return None


def build_hello(
    *,
    client_name: str = "hatari-mcp",
    client_version: str = "0.1.0",
    min_version: int = PROTOCOL_VERSION,
    max_version: int = PROTOCOL_VERSION,
    capabilities: Iterable[str] | None = None,
) -> bytes:
    if min_version < 1 or max_version < min_version:
        raise make_protocol_error(BAD_REQUEST, "Invalid HELLO version range.")
    caps, unknown = parse_capability_list(list(capabilities or CANONICAL))
    if unknown:
        raise make_protocol_error(BAD_REQUEST, "Unknown capability requested: " + ", ".join(unknown))
    return codec.encode_control(
        "hello",
        {
            "min_version": min_version,
            "max_version": max_version,
            "client": {"name": client_name, "version": client_version},
            "capabilities": sorted(caps),
        },
    )


def parse_hello(payload: bytes) -> dict[str, Any]:
    message = codec.decode_control(payload)
    if message["op"] != "hello":
        raise make_protocol_error(PROTOCOL, f"Expected hello, got {message['op']!r}.")
    params = message["params"]
    minimum = params.get("min_version")
    maximum = params.get("max_version")
    if not isinstance(minimum, int) or not isinstance(maximum, int) or minimum < 1 or maximum < minimum:
        raise make_protocol_error(PROTOCOL, "HELLO has an invalid version range.")
    caps, unknown = parse_capability_list(params.get("capabilities", []))
    return {
        "min_version": minimum,
        "max_version": maximum,
        "client": params.get("client") if isinstance(params.get("client"), dict) else {},
        "capabilities": sorted(caps),
        "unknown_capabilities": unknown,
    }


def build_hello_ack(
    *,
    version: int = PROTOCOL_VERSION,
    capabilities: Iterable[str],
    hatari_version: str | None = None,
    hatari_commit: str | None = None,
    build_flags: str | None = None,
    limits: dict[str, Any] | None = None,
) -> bytes:
    if version != PROTOCOL_VERSION:
        raise make_protocol_error(BAD_REQUEST, f"Invalid negotiated version {version}.")
    caps, unknown = parse_capability_list(list(capabilities))
    if unknown:
        raise make_protocol_error(BAD_REQUEST, "Unknown capability in ACK: " + ", ".join(unknown))
    result: dict[str, Any] = {
        "version": version,
        "capabilities": sorted(caps),
        "hatari_version": hatari_version,
        "hatari_commit": hatari_commit,
        "build_flags": build_flags,
        "limits": dict(limits or {}),
    }
    return codec.encode_ok(result)


def parse_hello_ack(payload: bytes) -> HelloAck:
    ok, result = codec.decode_response(payload)
    if not ok:
        raise make_protocol_error(PROTOCOL, "HELLO_ACK carried an error response.")
    if not isinstance(result, dict):
        raise make_protocol_error(PROTOCOL, "HELLO_ACK result must be an object.")
    version = result.get("version")
    if not isinstance(version, int) or version != PROTOCOL_VERSION:
        raise make_protocol_error(
            PROTOCOL,
            f"HELLO_ACK selected unsupported version {version!r}; expected {PROTOCOL_VERSION}.",
        )
    caps, unknown = parse_capability_list(result.get("capabilities", []))
    limits = result.get("limits")
    if limits is not None and not isinstance(limits, dict):
        raise make_protocol_error(PROTOCOL, "HELLO_ACK limits must be an object.")
    for key in ("hatari_version", "hatari_commit", "build_flags"):
        if result.get(key) is not None and not isinstance(result.get(key), str):
            raise make_protocol_error(PROTOCOL, f"HELLO_ACK {key} must be a string or null.")
    return HelloAck(
        version=version,
        capabilities=frozenset(caps),
        unknown_capabilities=tuple(unknown),
        hatari_version=result.get("hatari_version"),
        hatari_commit=result.get("hatari_commit"),
        build_flags=result.get("build_flags"),
        limits=limits,
    )


def negotiate_version(
    client_min: int,
    client_max: int,
    server_min: int = PROTOCOL_VERSION,
    server_max: int = PROTOCOL_VERSION,
) -> int:
    selected = min(client_max, server_max)
    if selected < max(client_min, server_min):
        raise make_protocol_error(
            PROTOCOL,
            "No mutually supported protocol version.",
            f"Client {client_min}..{client_max}, server {server_min}..{server_max}.",
        )
    return selected
