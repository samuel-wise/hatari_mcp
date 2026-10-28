"""Protocol mode selection driven by ``HATARI_MCP_PROTOCOL`` and the banner."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from ..errors import HatariError, error
from .capabilities import describe
from .hello import Banner, HelloAck
from . import capabilities as caps


class ProtocolMode(str, Enum):
    AUTO = "auto"
    LEGACY = "legacy"
    FRAMED = "framed"
    REQUIRE_FRAMED = "require-framed"


VALID_MODES = {mode.value for mode in ProtocolMode}


def parse_mode(value: str | ProtocolMode | None) -> ProtocolMode:
    if isinstance(value, ProtocolMode):
        return value
    normalized = (value or "auto").strip().lower()
    if normalized not in VALID_MODES:
        raise error(
            "bad_option",
            f"Invalid HATARI_MCP_PROTOCOL value: {value!r}",
            "Use auto, legacy, framed or require-framed.",
        )
    return ProtocolMode(normalized)


def mode_from_env(env: Mapping[str, str] | None = None) -> ProtocolMode:
    source = os.environ if env is None else env
    return parse_mode(source.get("HATARI_MCP_PROTOCOL", "auto"))


@dataclass
class Selection:
    requested: ProtocolMode
    mode: str
    banner: Banner | None
    ack: HelloAck | None = None
    negotiated: bool = False
    capabilities: dict[str, Any] = field(default_factory=dict)
    diagnostics: list[str] = field(default_factory=list)

    @property
    def protocol_mode(self) -> str:
        return self.mode

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested_mode": self.requested.value,
            "protocol_mode": self.mode,
            "negotiated": self.negotiated,
            "banner": self.banner.raw if self.banner else None,
            "capabilities": dict(self.capabilities),
            "diagnostics": list(self.diagnostics),
        }


def select_protocol(
    requested: str | ProtocolMode | None = "auto",
    *,
    banner: Banner | None = None,
    ack: HelloAck | None = None,
    hatari_version: str | None = None,
    diagnostics: list[str] | None = None,
) -> Selection:
    """Select the transport mode.

    A banner alone is advisory: until a real HELLO/HELLO_ACK completes the
    mode is ``banner-only`` and the legacy adapter is used. Only an ACK that
    selects v1 and advertises ``framed`` enables framed/native capabilities.
    ``framed``/``require-framed`` raise ``protocol_unavailable`` rather than
    silently downgrading.
    """
    mode = parse_mode(requested)
    notes = list(diagnostics or [])
    selected = "legacy"
    available = True
    handshake = ack is not None and mode is not ProtocolMode.LEGACY

    if mode is ProtocolMode.LEGACY:
        selected = "legacy"
        notes.append("Framing disabled by HATARI_MCP_PROTOCOL=legacy.")
    elif handshake:
        assert ack is not None
        if ack.version != 1:
            raise error(
                "protocol_unavailable",
                f"Hatari selected unsupported protocol version {ack.version}.",
                "Use a patched build that supports HMCP v1.",
            )
        if "framed" not in ack.capabilities:
            raise error(
                "protocol_unavailable",
                "HELLO_ACK did not include the framed capability.",
                "The patched Hatari is not usable as a framed transport.",
            )
        selected = "framed"
    elif banner is not None and "framed" in banner.capabilities:
        selected = "banner-only"
        notes.append("Banner seen but HELLO/HELLO_ACK has not completed; using legacy.")
    elif mode is ProtocolMode.AUTO:
        selected = "legacy"
        notes.append("No valid framed banner; using the stock legacy adapter.")
    else:
        available = False
        notes.append("Framed protocol requested but no valid HATARI-MCP-PROTOCOL v1 banner was seen.")

    if mode in {ProtocolMode.FRAMED, ProtocolMode.REQUIRE_FRAMED} and selected != "framed":
        available = False
        notes.append("Strict framed mode requires a completed HELLO/HELLO_ACK handshake.")

    if mode is ProtocolMode.REQUIRE_FRAMED:
        caps = ack.capabilities if ack is not None else frozenset()
        if selected != "framed" or "framed_prompt" not in caps:
            available = False
            notes.append("require-framed requires a completed handshake with framed_prompt.")

    if not available:
        raise error(
            "protocol_unavailable",
            f"{mode.value} mode requested but the framed protocol is unavailable.",
            "Use HATARI_MCP_PROTOCOL=auto/legacy, or a patched Hatari with a completed HMCP v1 handshake.",
            detail="; ".join(notes),
        )

    reported_caps = ack.capabilities if ack is not None and selected == "framed" else frozenset()
    report = describe(
        requested_mode=mode.value,
        banner=banner,
        negotiated_capabilities=reported_caps,
        handshake_complete=bool(ack is not None and selected == "framed"),
        hatari_version=ack.hatari_version if ack else hatari_version,
        hatari_commit=ack.hatari_commit if ack else None,
        limits=ack.limits if ack else None,
        diagnostics=notes,
        force_protocol_mode=selected,
    )
    return Selection(
        requested=mode,
        mode=selected,
        banner=banner,
        ack=ack,
        negotiated=ack is not None and selected == "framed",
        capabilities=report,
        diagnostics=notes,
    )


def strict_mode_requested(mode: str | ProtocolMode | None = None) -> bool:
    return parse_mode(mode) in {ProtocolMode.FRAMED, ProtocolMode.REQUIRE_FRAMED}


def selection_for_status(
    *,
    env: Mapping[str, str] | None = None,
    banner: Banner | None = None,
    ack: HelloAck | None = None,
    banner_error: str | None = None,
) -> dict[str, Any]:
    """Non-raising projection used by ``emu_status``/``emu_capabilities``."""
    try:
        mode = mode_from_env(env)
    except HatariError as exc:
        report = describe(requested_mode="auto", banner=banner, negotiated_capabilities=None)
        report["diagnostics"] = [str(exc)]
        report["protocol_available"] = False
        return report
    if mode in {ProtocolMode.FRAMED, ProtocolMode.REQUIRE_FRAMED} and banner is None:
        report = describe(requested_mode=mode.value, banner=None)
        if banner_error:
            report["diagnostics"].append(f"Banner rejected: {banner_error}")
        report["diagnostics"].append("Strict mode is unavailable; server startup would refuse.")
        return report
    reported = ack.capabilities if ack is not None else frozenset()
    report = describe(
        requested_mode=mode.value,
        banner=banner,
        negotiated_capabilities=reported,
        handshake_complete=ack is not None,
        hatari_version=ack.hatari_version if ack else None,
        hatari_commit=ack.hatari_commit if ack else None,
        limits=ack.limits if ack else None,
    )
    if banner_error and not any(banner_error in note for note in report["diagnostics"]):
        report["diagnostics"].append(f"Banner rejected: {banner_error}")
    return report


# Re-exported for tools that need the matrix without importing two modules.
LEGACY_STATES = caps.LEGACY_STATES
CANONICAL_CAPABILITIES = caps.CANONICAL_CAPABILITIES
