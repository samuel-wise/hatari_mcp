"""Canonical capability names and the stock/framed fallback matrix."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CANONICAL_CAPABILITIES: tuple[str, ...] = (
    "framed",
    "framed_prompt",
    "notify",
    "binary",
    "fb_native",
    "input_frame",
    "input_abs",
    "input_joy",
    "frame_step",
    "clock_virtual",
    "windows_pipe",
    "headless_render",
)
CANONICAL = frozenset(CANONICAL_CAPABILITIES)

NATIVE = "native"
FALLBACK = "fallback"
UNSUPPORTED = "unsupported"

# State for a stock, unpatched Hatari. Every fallback is explicitly labelled.
LEGACY_STATES: dict[str, str] = {
    "framed": UNSUPPORTED,
    "framed_prompt": UNSUPPORTED,
    "notify": FALLBACK,
    "binary": FALLBACK,
    "fb_native": FALLBACK,
    "input_frame": UNSUPPORTED,
    "input_abs": UNSUPPORTED,
    "input_joy": FALLBACK,
    "frame_step": FALLBACK,
    "clock_virtual": UNSUPPORTED,
    "windows_pipe": UNSUPPORTED,
    "headless_render": FALLBACK,
}

LEGACY_NOTES: dict[str, str] = {
    "notify": "Breakpoints/exceptions are inferred from stderr; manual entry may be reported as unknown.",
    "binary": "Uses savebin to a confined temporary file or capped memdump text.",
    "fb_native": "Uses the debugger screenshot command plus server-side image decoding.",
    "input_joy": "Keyboard-emulated joystick only; no true axes.",
    "frame_step": "Approximate: _vbl breakpoint / cont or --run-vbls; may overshoot and cannot freeze the clock.",
    "headless_render": "SDL_VIDEODRIVER=dummy keeps rendering headless; no native --headless patch.",
}

FRAMED_NOTE = "Negotiated HMCP v1 capability."

DEFAULT_LIMITS: dict[str, Any] = {
    "max_payload": 8 * 1024 * 1024,
    "absolute_max_payload": 64 * 1024 * 1024,
    "protocol_version": 1,
}


@dataclass
class CapabilityReport:
    requested_mode: str
    protocol_mode: str
    negotiated: bool
    protocol_available: bool
    version: int | None
    states: dict[str, str]
    reported_capabilities: list[str] = field(default_factory=list)
    advertised_capabilities: list[str] = field(default_factory=list)
    hatari_version: str | None = None
    hatari_commit: str | None = None
    limits: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_LIMITS))
    diagnostics: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        # ``capabilities`` and ``available`` mean "usable somehow (native or
        # documented fallback)". ``native`` is the only handshake-gated truth.
        capabilities = {name: self.states[name] != UNSUPPORTED for name in CANONICAL_CAPABILITIES}
        native = {name: self.states[name] == NATIVE for name in CANONICAL_CAPABILITIES}
        return {
            "ok": True,
            "requested_mode": self.requested_mode,
            "protocol_mode": self.protocol_mode,
            "negotiated": self.negotiated,
            "protocol_available": self.protocol_available,
            "version": self.version,
            "hatari": {
                "version": self.hatari_version,
                "commit": self.hatari_commit,
            },
            "capabilities": capabilities,
            "available": dict(capabilities),
            "native": native,
            "states": dict(self.states),
            "unsupported": [name for name in CANONICAL_CAPABILITIES if self.states[name] == UNSUPPORTED],
            "degraded": [name for name in CANONICAL_CAPABILITIES if self.states[name] == FALLBACK],
            "reported_capabilities": list(self.reported_capabilities),
            "advertised_capabilities": list(self.advertised_capabilities),
            "limits": dict(self.limits),
            "diagnostics": list(self.diagnostics),
        }


def normalize_reported(capabilities: object) -> tuple[set[str], list[str]]:
    """Split a reported capability list into canonical and unknown names."""
    if capabilities is None:
        return set(), []
    if isinstance(capabilities, str):
        raw = [part.strip() for part in capabilities.split(",")]
    elif isinstance(capabilities, (list, tuple, set, frozenset)):
        raw = [str(part).strip() for part in capabilities]
    else:
        return set(), []
    known = {item for item in raw if item in CANONICAL}
    unknown = sorted(item for item in raw if item and item not in CANONICAL)
    return known, unknown


def describe(
    *,
    requested_mode: str = "auto",
    banner: object = None,
    negotiated_capabilities: object = None,
    handshake_complete: bool = False,
    hatari_version: str | None = None,
    hatari_commit: str | None = None,
    limits: dict[str, Any] | None = None,
    diagnostics: list[str] | None = None,
    force_protocol_mode: str | None = None,
) -> dict[str, Any]:
    """Build the stable ``emu_capabilities`` projection.

    A startup banner is only advisory. Native capabilities require an actual
    HELLO/HELLO_ACK handshake (``handshake_complete=True`` and ``framed`` in
    ``negotiated_capabilities``). A banner without a handshake is reported as
    ``banner-only`` while the legacy adapter is still in use. No banner means
    legacy.
    """
    diagnostics = list(diagnostics or [])
    banner_caps: set[str] = set()
    banner_version: int | None = None
    if banner is not None:
        banner_caps, unknown = normalize_reported(getattr(banner, "capabilities", banner))
        banner_version = getattr(banner, "version", None)
        for extra in getattr(banner, "unknown_capabilities", ()) or ():
            if isinstance(extra, str) and extra not in unknown:
                unknown.append(extra)
        if unknown:
            diagnostics.append("Unknown banner capabilities ignored: " + ", ".join(sorted(unknown)))

    negotiated, unknown_negotiated = normalize_reported(negotiated_capabilities)
    if unknown_negotiated:
        diagnostics.append("Unknown negotiated capabilities ignored: " + ", ".join(unknown_negotiated))

    strict = requested_mode in {"framed", "require-framed"}
    require_prompt = requested_mode == "require-framed"
    banner_offers_framed = banner is not None and "framed" in banner_caps
    handshake_framed = bool(handshake_complete and "framed" in negotiated)

    if requested_mode == "legacy":
        mode = "legacy"
        available = True
        diagnostics.append("HATARI_MCP_PROTOCOL=legacy: framing is disabled.")
    elif handshake_framed:
        mode = "framed"
        available = True
    elif banner_offers_framed:
        mode = "banner-only"
        available = not strict
        if strict:
            diagnostics.append(
                "Startup banner observed but the HELLO/HELLO_ACK handshake did not complete; "
                "strict framed mode is unavailable."
            )
        else:
            diagnostics.append(
                "Startup banner observed but the framed handshake is not wired; using the legacy adapter."
            )
    elif strict:
        mode = "legacy"
        available = False
        diagnostics.append("Framed protocol requested but no valid v1 framed banner was observed.")
    else:
        mode = "legacy"
        available = True
        if banner is not None:
            diagnostics.append("Banner did not advertise 'framed'; using the stock legacy adapter.")
        else:
            diagnostics.append("No HATARI-MCP-PROTOCOL banner; using the stock legacy adapter.")

    if force_protocol_mode is not None:
        mode = force_protocol_mode

    if mode == "framed":
        states = dict(LEGACY_STATES)
        states["framed"] = NATIVE
        for name in CANONICAL_CAPABILITIES:
            if name in negotiated:
                states[name] = NATIVE
        if "framed_prompt" not in negotiated:
            states["framed_prompt"] = UNSUPPORTED
        if require_prompt and states["framed_prompt"] != NATIVE:
            available = False
            diagnostics.append("require-framed also requires framed_prompt, which was not negotiated.")
        version = banner_version or 1
        negotiated_flag = True
        reported = sorted(negotiated)
        advertised = sorted(banner_caps)
    else:
        states = dict(LEGACY_STATES)
        version = banner_version if banner is not None else None
        negotiated_flag = False
        reported = sorted(banner_caps) if mode == "banner-only" else []
        advertised = sorted(banner_caps)
        if strict:
            states["framed"] = UNSUPPORTED

    report = CapabilityReport(
        requested_mode=requested_mode,
        protocol_mode=mode,
        negotiated=negotiated_flag,
        protocol_available=available,
        version=version,
        states=states,
        reported_capabilities=reported,
        advertised_capabilities=advertised,
        hatari_version=hatari_version,
        hatari_commit=hatari_commit,
        limits=dict(limits or DEFAULT_LIMITS),
        diagnostics=diagnostics,
    )
    return report.as_dict()


def legacy_report(
    *,
    requested_mode: str = "auto",
    diagnostics: list[str] | None = None,
    hatari_version: str | None = None,
) -> dict[str, Any]:
    return describe(
        requested_mode=requested_mode,
        diagnostics=diagnostics,
        hatari_version=hatari_version,
    )


def notes_for(states: dict[str, str]) -> dict[str, str]:
    """Human-readable notes for the non-native states (used by docs/tests)."""
    out: dict[str, str] = {}
    for name, state in states.items():
        if state == NATIVE:
            out[name] = FRAMED_NOTE
        elif state == FALLBACK:
            out[name] = LEGACY_NOTES.get(name, "Stock fallback.")
        else:
            out[name] = "Unsupported by this build."
    return out
