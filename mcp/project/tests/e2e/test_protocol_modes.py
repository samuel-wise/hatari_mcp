"""Drive the stdio MCP server and inspect capability/status surfaces."""

from __future__ import annotations

import asyncio
import json
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from hatari_mcp.config import Settings

SETTINGS = Settings.load()
PREREQS = SETTINGS.hatari_bin is not None and SETTINGS.default_tos is not None
pytestmark = pytest.mark.skipif(not PREREQS, reason="Hatari binary or TOS image is unavailable")


def tool_result(result) -> dict:
    if result.structured_content is not None:
        return result.structured_content
    for item in result.content:
        if getattr(item, "type", None) == "text":
            return json.loads(item.text)
    raise AssertionError(f"tool returned no structured result: {result!r}")


async def call(session: ClientSession, name: str, arguments: dict) -> dict:
    return tool_result(await session.call_tool(name, arguments))


def test_stdio_capabilities_and_lifecycle() -> None:
    async def main() -> None:
        parameters = StdioServerParameters(command=sys.executable, args=["-m", "hatari_mcp"])
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools = {tool.name for tool in (await session.list_tools()).tools}
                assert "emu_status" in tools
                status = await call(session, "emu_status", {})
                assert "capabilities" in status
                capabilities = status["capabilities"]
                assert capabilities["protocol_mode"] == "legacy"
                assert capabilities["capabilities"]["framed"] is False

                capability_tool = "emu_capabilities" in tools
                if capability_tool:
                    reported = await call(session, "emu_capabilities", {})
                    assert set(reported) >= {"capabilities", "states", "unsupported", "degraded"}
                    assert reported["protocol_mode"] == "legacy"
                    assert set(reported) == set(capabilities) | {
                        "canonical_capabilities",
                        "selection",
                        "server_version",
                    }

                started = await call(session, "emu_start", {})
                assert started["state"] == "running"
                try:
                    running = await call(session, "emu_status", {})
                    assert running["state"] == "running"
                    assert running["pid"]
                    assert running["capabilities"]["capabilities"]["framed"] is False
                finally:
                    stopped = await call(session, "emu_stop", {"force": True})
                assert stopped["state"] == "exited"
                assert stopped["socket"] is None

    asyncio.run(main())
