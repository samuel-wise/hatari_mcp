"""Wire error codes and their mapping to MCP/Hatari errors."""

from __future__ import annotations

from typing import Any

from ..errors import HatariError, error

# Stable wire codes. Do not renumber or remove; add new codes at the end.
BAD_REQUEST = "bad_request"
UNKNOWN_OP = "unknown_op"
UNSUPPORTED = "unsupported"
NO_FRAMEBUFFER = "no_framebuffer"
OUT_OF_RANGE = "out_of_range"
BUSY = "busy"
QUEUE_FULL = "queue_full"
PROTOCOL = "protocol"
INTERNAL = "internal"
TIMEOUT = "timeout"
TRUNCATED = "truncated_frame"
TOO_LARGE = "frame_too_large"
BAD_FRAME = "bad_frame"
GAP = "notification_gap"

WIRE_CODES = frozenset(
    {
        BAD_REQUEST,
        UNKNOWN_OP,
        UNSUPPORTED,
        NO_FRAMEBUFFER,
        OUT_OF_RANGE,
        BUSY,
        QUEUE_FULL,
        PROTOCOL,
        INTERNAL,
        TIMEOUT,
        TRUNCATED,
        TOO_LARGE,
        BAD_FRAME,
        GAP,
    }
)

# Wire code -> public HatariError code. Keep this explicit so a new wire code
# never silently becomes an untyped exception.
WIRE_TO_HATARI = {
    BAD_REQUEST: "bad_option",
    UNKNOWN_OP: "unsupported",
    UNSUPPORTED: "unsupported",
    NO_FRAMEBUFFER: "unsupported",
    OUT_OF_RANGE: "bad_option",
    BUSY: "busy",
    QUEUE_FULL: "busy",
    PROTOCOL: "protocol_error",
    INTERNAL: "internal_error",
    TIMEOUT: "timeout",
    TRUNCATED: "protocol_error",
    TOO_LARGE: "protocol_error",
    BAD_FRAME: "protocol_error",
    GAP: "protocol_error",
}


class ProtocolError(HatariError):
    """A framed-protocol failure with a stable wire code."""

    def __init__(
        self,
        code: str,
        message: str,
        hint: str = "",
        *,
        data: Any = None,
        raw: str | None = None,
    ):
        super().__init__(code=code, message=message, hint=hint, raw=raw)
        self.data = data

    def as_wire(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.hint:
            payload["hint"] = self.hint
        if self.data is not None:
            payload["data"] = self.data
        return payload

    def as_dict(self) -> dict[str, Any]:
        result = super().as_dict()
        result["error"] = self.code
        result["wire_code"] = self.code
        result["wire"] = self.as_wire()
        return result


def wire_error(code: str, message: str, hint: str = "", *, data: Any = None) -> ProtocolError:
    return ProtocolError(code if code in WIRE_CODES else INTERNAL, message, hint, data=data)


def make_protocol_error(code: str, message: str, hint: str = "") -> ProtocolError:
    return ProtocolError(code if code in WIRE_CODES else INTERNAL, message, hint)


def from_wire(payload: Any) -> ProtocolError:
    """Convert a decoded wire error object into a typed exception."""
    if not isinstance(payload, dict):
        return ProtocolError(PROTOCOL, "Malformed wire error payload.")
    code = payload.get("code")
    if not isinstance(code, str) or not code:
        code = INTERNAL
    message = payload.get("message")
    if not isinstance(message, str) or not message:
        message = "Protocol error."
    hint = payload.get("hint")
    if not isinstance(hint, str):
        hint = ""
    return ProtocolError(code, message, hint, data=payload.get("data"))


def to_mcp_error(exc: ProtocolError) -> HatariError:
    """Map a wire-level failure to the server's public error vocabulary."""
    mapped = WIRE_TO_HATARI.get(exc.code, "internal_error")
    return error(mapped, exc.message, exc.hint, raw=exc.raw)
