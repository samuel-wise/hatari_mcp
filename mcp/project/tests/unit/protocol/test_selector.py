"""HATARI_MCP_PROTOCOL selection and strict-mode refusal."""

from __future__ import annotations

import pytest

from hatari_mcp.errors import HatariError
from hatari_mcp.protocol.hello import Banner, HelloAck
from hatari_mcp.protocol.selector import (
    ProtocolMode,
    mode_from_env,
    parse_mode,
    select_protocol,
    selection_for_status,
)


def banner(*caps: str) -> Banner:
    return Banner(1, frozenset(caps))


def test_mode_from_env_defaults_and_validation() -> None:
    assert mode_from_env({}) is ProtocolMode.AUTO
    assert mode_from_env({"HATARI_MCP_PROTOCOL": "LEGACY"}) is ProtocolMode.LEGACY
    with pytest.raises(HatariError):
        mode_from_env({"HATARI_MCP_PROTOCOL": "bogus"})
    with pytest.raises(HatariError):
        parse_mode("nope")


def test_auto_uses_legacy_without_banner() -> None:
    selection = select_protocol("auto")
    assert selection.mode == "legacy"
    assert selection.capabilities["diagnostics"]
    assert selection.capabilities["protocol_available"] is True


def test_auto_banner_without_handshake_is_banner_only() -> None:
    selection = select_protocol("auto", banner=banner("framed", "notify"))
    assert selection.mode == "banner-only"
    assert selection.negotiated is False
    assert selection.capabilities["states"]["notify"] == "fallback"
    assert not any(selection.capabilities["native"].values())


def test_legacy_ignores_a_banner() -> None:
    selection = select_protocol("legacy", banner=banner("framed", "notify"))
    assert selection.mode == "legacy"
    assert selection.capabilities["states"]["framed"] == "unsupported"


def test_framed_requires_banner_and_handshake() -> None:
    with pytest.raises(HatariError) as excinfo:
        select_protocol("framed")
    assert excinfo.value.code == "protocol_unavailable"
    with pytest.raises(HatariError):
        select_protocol("framed", banner=banner("framed", "notify"))


def test_require_framed_requires_a_real_handshake() -> None:
    with pytest.raises(HatariError) as excinfo:
        select_protocol("require-framed", banner=banner("framed", "notify"))
    assert excinfo.value.code == "protocol_unavailable"
    with pytest.raises(HatariError):
        select_protocol("require-framed", banner=banner("framed", "framed_prompt"), ack=None)
    ack = HelloAck(
        version=1,
        capabilities=frozenset({"framed", "framed_prompt"}),
        unknown_capabilities=(),
    )
    selection = select_protocol(
        "require-framed", banner=banner("framed", "framed_prompt"), ack=ack
    )
    assert selection.mode == "framed"


def test_legacy_ignores_ack_too() -> None:
    ack = HelloAck(version=1, capabilities=frozenset({"framed", "framed_prompt"}), unknown_capabilities=())
    selection = select_protocol("legacy", banner=banner("framed"), ack=ack)
    assert selection.mode == "legacy"
    assert selection.negotiated is False


def test_ack_selects_framed_and_reports_negotiated() -> None:
    ack = HelloAck(
        version=1,
        capabilities=frozenset({"framed", "framed_prompt", "notify"}),
        unknown_capabilities=(),
        hatari_version="2.6.1",
    )
    selection = select_protocol("auto", banner=banner("framed"), ack=ack)
    assert selection.mode == "framed"
    assert selection.negotiated is True
    assert selection.capabilities["hatari"]["version"] == "2.6.1"


def test_ack_without_framed_is_refused() -> None:
    ack = HelloAck(version=1, capabilities=frozenset({"notify"}), unknown_capabilities=())
    with pytest.raises(HatariError):
        select_protocol("framed", banner=banner("framed"), ack=ack)


def test_status_projection_never_raises_for_strict_mode() -> None:
    report = selection_for_status(env={"HATARI_MCP_PROTOCOL": "framed"}, banner=None)
    assert report["protocol_available"] is False
    assert any("Strict mode" in note for note in report["diagnostics"])
