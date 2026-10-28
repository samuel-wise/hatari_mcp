"""Startup banner and HELLO/HELLO_ACK negotiation."""

from __future__ import annotations

import pytest

from hatari_mcp.protocol import codec
from hatari_mcp.protocol.errors import ProtocolError
from hatari_mcp.protocol.hello import (
    build_hello,
    build_hello_ack,
    find_banner,
    negotiate_version,
    parse_banner_line,
    parse_hello,
    parse_hello_ack,
)


def test_parse_banner() -> None:
    banner = parse_banner_line("HATARI-MCP-PROTOCOL 1 capabilities=framed,notify,binary")
    assert banner is not None
    assert banner.version == 1
    assert banner.capabilities == frozenset({"framed", "notify", "binary"})
    assert banner.unknown_capabilities == ()


def test_parse_banner_without_capabilities_and_unknown_names() -> None:
    banner = parse_banner_line(" HATARI-MCP-PROTOCOL 1 ")
    assert banner is not None and banner.capabilities == frozenset()
    banner = parse_banner_line("HATARI-MCP-PROTOCOL 1 capabilities=framed,future_thing")
    assert banner is not None
    assert banner.capabilities == frozenset({"framed"})
    assert banner.unknown_capabilities == ("future_thing",)


def test_non_banner_lines_and_bad_version() -> None:
    assert parse_banner_line("INFO: hello") is None
    with pytest.raises(ProtocolError):
        parse_banner_line("HATARI-MCP-PROTOCOL 0 capabilities=framed")


def test_find_banner_in_stream_text() -> None:
    text = "INFO: boot\nHATARI-MCP-PROTOCOL 1 capabilities=framed_prompt\nINFO: ready\n"
    banner = find_banner(text)
    assert banner is not None and "framed_prompt" in banner.capabilities


def test_hello_round_trip() -> None:
    payload = build_hello(client_name="test", client_version="1.2", capabilities=["framed"])
    parsed = parse_hello(payload)
    assert parsed["min_version"] == 1
    assert parsed["max_version"] == 1
    assert parsed["capabilities"] == ["framed"]


def test_hello_rejects_bad_version_range() -> None:
    with pytest.raises(ProtocolError):
        build_hello(min_version=2, max_version=1)
    bad = codec.encode_control("hello", {"min_version": 2, "max_version": 1})
    with pytest.raises(ProtocolError):
        parse_hello(bad)


def test_hello_ack_round_trip() -> None:
    payload = build_hello_ack(
        capabilities=["framed", "notify", "framed_prompt"],
        hatari_version="2.6.1",
        hatari_commit="abc",
        limits={"max_payload": 1024},
    )
    ack = parse_hello_ack(payload)
    assert ack.version == 1
    assert ack.capabilities == frozenset({"framed", "notify", "framed_prompt"})
    assert ack.hatari_version == "2.6.1"
    assert ack.limits == {"max_payload": 1024}


def test_hello_ack_rejects_unknown_capability_and_bad_version() -> None:
    with pytest.raises(ProtocolError):
        build_hello_ack(capabilities=["framed", "nope"])
    with pytest.raises(ProtocolError):
        parse_hello_ack(codec.encode_ok({"version": 0, "capabilities": []}))


def test_negotiate_version() -> None:
    assert negotiate_version(1, 1) == 1
    assert negotiate_version(1, 3, 2, 4) == 3
    with pytest.raises(ProtocolError):
        negotiate_version(2, 3, 1, 1)


def test_banner_versions_and_nonce_are_strict() -> None:
    with pytest.raises(ProtocolError):
        parse_banner_line("HATARI-MCP-PROTOCOL 2 capabilities=framed")
    with pytest.raises(ProtocolError):
        parse_banner_line("HATARI-MCP-PROTOCOL 1 capabilities=framed", expected_nonce="abc")
    banner = parse_banner_line(
        "HATARI-MCP-PROTOCOL 1 capabilities=framed nonce=abc", expected_nonce="abc"
    )
    assert banner is not None and banner.version == 1
    with pytest.raises(ProtocolError):
        parse_banner_line(
            "HATARI-MCP-PROTOCOL 1 capabilities=framed nonce=abc", expected_nonce="def"
        )


def test_hello_ack_version_must_be_exactly_one() -> None:
    with pytest.raises(ProtocolError):
        parse_hello_ack(codec.encode_ok({"version": 2, "capabilities": ["framed"]}))
    with pytest.raises(ProtocolError):
        build_hello_ack(version=2, capabilities=["framed"])
