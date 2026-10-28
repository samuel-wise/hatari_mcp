"""Bearer-token and origin helpers for the optional HTTP/SSE transport."""

from __future__ import annotations

import hmac
import ipaddress
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

TOKEN_ENV = "HATARI_MCP_HTTP_" "TOKEN"
TOKEN_FILE_ENV = "HATARI_MCP_HTTP_" "TOKEN_FILE"
MIN_TOKEN_BYTES = 16
BEARER_RE = re.compile(r"^Bearer[ \t]+(?P<token>[^\s]+)$")
SESSION_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")


def is_loopback(host: str) -> bool:
    host = (host or "").strip().strip("[]").lower()
    if host in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def load_token(
    *,
    token: str | None = None,
    token_file: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    generate: bool = True,
) -> str | None:
    """Load a token from explicit value, env or a 0600 file.

    An explicitly supplied token must be at least 16 bytes. Files must not be
    group/world readable. Returns ``None`` only when no source exists and
    ``generate`` is false.
    """
    source = os.environ if env is None else env
    value = token or source.get(TOKEN_ENV)
    path_value = token_file or source.get(TOKEN_FILE_ENV)
    if value is None and path_value:
        path = Path(path_value).expanduser()
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError as exc:
            raise ValueError(f"Cannot read token file {path}: {exc}") from exc
        if mode & 0o077:
            raise ValueError(f"Token file {path} must not be group/world accessible (mode {mode:o}).")
        value = path.read_text(encoding="utf-8").strip()
    if value is not None:
        if len(value.encode("utf-8")) < MIN_TOKEN_BYTES:
            raise ValueError(f"HTTP token must be at least {MIN_TOKEN_BYTES} bytes.")
        return value
    return generate_token() if generate else None


def parse_bearer(header: str | None) -> str | None:
    if not isinstance(header, str):
        return None
    match = BEARER_RE.match(header.strip())
    return match.group("token") if match else None


def token_matches(expected: str | None, supplied: str | None) -> bool:
    if not expected or not supplied:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8"))


def validate_session_id(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value if SESSION_RE.match(value) else None


def origin_allowed(origin: str | None, allowed_origins: frozenset[str] | None = None) -> bool:
    """Missing Origin is allowed for non-browser clients.

    A browser Origin must match an explicit allowlist or be loopback. ``null``
    (sandboxed iframes/file URLs) is always rejected.
    """
    if origin is None or origin == "":
        return True
    if origin.strip().lower() == "null":
        return False
    if allowed_origins and origin in allowed_origins:
        return True
    match = re.match(r"^https?://(?P<host>\[[^\]]+\]|[^/:]+)(?::\d+)?$", origin.strip())
    return bool(match and is_loopback(match.group("host")))


def host_allowed(host_header: str | None, allowed_hosts: frozenset[str] | None = None) -> bool:
    if not host_header:
        return False
    host = host_header.strip().lower()
    if allowed_hosts and host in {item.lower() for item in allowed_hosts}:
        return True
    name = host
    if name.startswith("["):
        name = name[1:].split("]", 1)[0]
    elif name.count(":") == 1:
        name = name.rsplit(":", 1)[0]
    return is_loopback(name)


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    safe: dict[str, str] = {}
    for key, value in headers.items():
        lowered = key.lower()
        if lowered in {"authorization", "cookie", "proxy-authorization", "set-cookie", "x-api-key"}:
            safe[key] = "<redacted>"
        else:
            safe[key] = value
    return safe


def redact_text(value: object, tokens: tuple[str, ...] = ()) -> str:
    text = str(value)
    for token in tokens:
        if token:
            text = text.replace(token, "<redacted>")
    return text


@dataclass(frozen=True)
class AuthDecision:
    ok: bool
    status: int
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "status": self.status, "reason": self.reason}


def authorize(
    *,
    expected_token: str | None,
    authorization: str | None,
    origin: str | None = None,
    host: str | None = None,
    allowed_origins: frozenset[str] | None = None,
    allowed_hosts: frozenset[str] | None = None,
    require_custom_header: bool = False,
    custom_header: str | None = None,
) -> AuthDecision:
    if not token_matches(expected_token, parse_bearer(authorization)):
        return AuthDecision(False, 401, "missing or invalid bearer token")
    if not host_allowed(host, allowed_hosts):
        return AuthDecision(False, 403, "host not allowed")
    if not origin_allowed(origin, allowed_origins):
        return AuthDecision(False, 403, "origin not allowed")
    if require_custom_header and not custom_header:
        return AuthDecision(False, 403, "missing anti-CSRF header")
    return AuthDecision(True, 200, "ok")
