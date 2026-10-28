"""Capability matrix: truthful stock fallback and identical schemas."""

from __future__ import annotations

from hatari_mcp.protocol.capabilities import CANONICAL_CAPABILITIES, describe, notes_for
from hatari_mcp.protocol.hello import Banner

EXPECTED_KEYS = {
    "ok",
    "requested_mode",
    "protocol_mode",
    "negotiated",
    "protocol_available",
    "version",
    "hatari",
    "capabilities",
    "available",
    "native",
    "states",
    "unsupported",
    "degraded",
    "reported_capabilities",
    "advertised_capabilities",
    "limits",
    "diagnostics",
}


def test_legacy_report_is_truthful() -> None:
    report = describe(requested_mode="auto")
    assert set(report) == EXPECTED_KEYS
    assert report["protocol_mode"] == "legacy"
    assert report["version"] is None
    assert report["capabilities"]["framed"] is False
    assert report["capabilities"]["binary"] is True  # fallback exists
    assert report["native"]["binary"] is False
    assert report["states"]["framed"] == "unsupported"
    assert report["states"]["binary"] == "fallback"
    assert "framed_prompt" in report["unsupported"]
    assert "binary" in report["degraded"]


def test_banner_alone_is_advisory_and_never_native() -> None:
    banner = Banner(1, frozenset({"framed", "framed_prompt", "notify", "binary"}))
    report = describe(
        requested_mode="auto",
        banner=banner,
        negotiated_capabilities=banner.capabilities,
        handshake_complete=False,
    )
    assert report["protocol_mode"] == "banner-only"
    assert report["negotiated"] is False
    assert report["states"]["framed"] == "unsupported"
    assert report["states"]["notify"] == "fallback"
    assert not any(report["native"].values())
    assert report["capabilities"]["framed"] is False
    assert report["advertised_capabilities"] == ["binary", "framed", "framed_prompt", "notify"]
    assert set(report) == EXPECTED_KEYS


def test_completed_handshake_grants_only_negotiated_native_capabilities() -> None:
    banner = Banner(1, frozenset({"framed", "framed_prompt", "notify", "binary"}))
    report = describe(
        requested_mode="auto",
        banner=banner,
        negotiated_capabilities=banner.capabilities,
        handshake_complete=True,
    )
    assert report["protocol_mode"] == "framed"
    assert report["negotiated"] is True
    assert report["states"]["framed"] == "native"
    assert report["states"]["notify"] == "native"
    assert report["states"]["fb_native"] == "fallback"
    assert report["native"]["binary"] is True
    assert set(report) == EXPECTED_KEYS


def test_schema_identical_between_legacy_and_framed() -> None:
    legacy = describe(requested_mode="legacy")
    framed = describe(
        requested_mode="auto",
        banner=Banner(1, frozenset({"framed", "framed_prompt"})),
        negotiated_capabilities=["framed", "framed_prompt"],
        handshake_complete=True,
    )
    assert set(legacy) == set(framed)
    assert set(legacy["states"]) == set(CANONICAL_CAPABILITIES)
    assert set(framed["states"]) == set(CANONICAL_CAPABILITIES)


def test_strict_mode_without_banner_is_unavailable_but_does_not_lie() -> None:
    report = describe(requested_mode="framed")
    assert report["protocol_available"] is False
    assert report["protocol_mode"] == "legacy"
    assert report["states"]["framed"] == "unsupported"
    assert any("framed protocol requested" in note.lower() for note in report["diagnostics"])


def test_require_framed_without_prompt_capability_is_unavailable() -> None:
    banner = Banner(1, frozenset({"framed", "notify"}))
    report = describe(requested_mode="require-framed", banner=banner, negotiated_capabilities=banner.capabilities)
    assert report["protocol_available"] is False
    assert report["states"]["framed_prompt"] == "unsupported"


def test_unknown_capability_is_reported_not_silently_ignored() -> None:
    banner = Banner(1, frozenset({"framed"}), ("future",))
    report = describe(requested_mode="auto", banner=banner)
    assert any("future" in note for note in report["diagnostics"])
    assert "future" not in report["states"]


def test_notes_cover_every_state() -> None:
    report = describe(requested_mode="auto")
    notes = notes_for(report["states"])
    assert set(notes) == set(CANONICAL_CAPABILITIES)
    assert "savebin" in notes["binary"].lower() or "temporary" in notes["binary"].lower()
