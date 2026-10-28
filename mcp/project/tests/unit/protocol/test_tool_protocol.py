"""The emu_capabilities tool exposes the same schema as status."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from hatari_mcp.tools import protocol


class FakeServer:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, **kwargs: Any):
        def decorator(function):
            self.tools[kwargs["name"]] = function
            return function

        return decorator


def make_app(banner=None, raw=None):
    return SimpleNamespace(
        supervisor=SimpleNamespace(protocol_banner=banner, protocol_banner_raw=raw)
    )


def test_register_and_call_emu_capabilities() -> None:
    server = FakeServer()
    app = make_app()
    protocol.register(server, app)
    assert "emu_capabilities" in server.tools
    result = asyncio.run(server.tools["emu_capabilities"]())
    assert result["protocol_mode"] == "legacy"
    assert result["capabilities"]["framed"] is False
    assert result["canonical_capabilities"]
    assert result["selection"]["requested_mode"] == "auto"


def test_mode_override_is_reported_without_starting_transport() -> None:
    server = FakeServer()
    app = make_app()
    protocol.register(server, app)
    result = asyncio.run(server.tools["emu_capabilities"](mode="framed"))
    assert result["requested_mode"] == "framed"
    assert result["protocol_available"] is False


def test_invalid_mode_is_a_tool_error() -> None:
    server = FakeServer()
    app = make_app()
    protocol.register(server, app)
    with pytest.raises(ToolError):
        asyncio.run(server.tools["emu_capabilities"](mode="bogus"))
