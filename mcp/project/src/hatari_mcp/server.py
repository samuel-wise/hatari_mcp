"""MCP server wiring and stdio entry point."""

from __future__ import annotations

import json
import logging
import sys
from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer

from . import __version__
from .app import App
from .tools import (
    automation,
    build,
    debug,
    debug_advanced,
    debug_extra,
    devices,
    frames,
    input,
    lifecycle,
    logs,
    machine,
    media,
    memory,
    memory_extra,
    protocol,
    screen,
    scripting,
)

logger = logging.getLogger("hatari_mcp")

APP = App()


@asynccontextmanager
async def lifespan(_server: MCPServer):
    try:
        yield APP
    finally:
        await APP.shutdown()


server = MCPServer(
    "hatari-mcp",
    version=__version__,
    description=(
        "Control a headless Hatari Atari ST/STE/TT/Falcon emulator and its debugger. "
        "The server owns one child process and routes commands over a private control socket "
        "or, when stopped at the debugger prompt, over Hatari's stdin."
    ),
    lifespan=lifespan,
    log_level="INFO",
)

lifecycle.register(server, APP)
logs.register(server, APP)
machine.register(server, APP)
debug.register(server, APP)
debug_extra.register(server, APP)
debug_advanced.register(server, APP)
memory.register(server, APP)
memory_extra.register(server, APP)
protocol.register(server, APP)
screen.register(server, APP)
frames.register(server, APP)
input.register(server, APP)
scripting.register(server, APP)
media.register(server, APP)
devices.register(server, APP)
build.register(server, APP)
automation.register(server, APP)


@server.resource(
    "hatari://server/status",
    name="server-status",
    description="Current emulator lifecycle, process and debugger state as JSON.",
    mime_type="application/json",
)
async def server_status() -> str:
    return json.dumps(APP.status(), indent=2)


@server.resource(
    "hatari://logs/recent",
    name="recent-logs",
    description="Recent classified stderr output (Hatari logs and debugger output).",
    mime_type="text/plain",
)
async def recent_logs() -> str:
    entries = APP.supervisor.stderr_ring.tail(100)
    return "\n".join(f"[{entry.kind}] {entry.text}" for entry in entries)


@server.resource(
    "hatari://docs/debugger",
    name="debugger-help",
    description="Key constraints of Hatari's stock debugger for MCP clients.",
    mime_type="text/markdown",
)
async def debugger_help() -> str:
    return (
        "# Hatari debugger channel notes\n\n"
        "- While Hatari is at the debugger prompt it reads stdin; control-socket commands queue.\n"
        "- `dbg_command` channel=auto uses stdin at the prompt and the socket while running.\n"
        "- `step`/`next` stay at the prompt; `cont`/`quit`/`reset` leave it.\n"
        "- Breakpoints need a condition, e.g. `b pc = $fc6600 :once`.\n"
        "- `savebin`/`loadbin` are the exact memory read/write paths.\n"
        "- Pagers are disabled by the generated config; a pager prompt is reported as pager_blocked.\n"
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    server.run(transport="stdio")
