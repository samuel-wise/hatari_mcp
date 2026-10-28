"""Watchpoint capability reporting for stock Hatari."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..app import App
from ..errors import error
from ._common import guarded

WATCHPOINT_LIMITATION = (
    "Stock Hatari 2.6.1-devel has no native watchpoint object. "
    "Its conditional CPU breakpoints are evaluated between instructions and cannot "
    "reliably observe reads or every write, so a polling fallback would report false "
    "negatives and race with a running CPU. This server does not fake watchpoints."
)
WATCHPOINT_HINT = (
    "Use dbg_breakpoint_set/dbg_breakpoint_wait with an explicit value condition "
    "(for example '(0x1234).w = 0xffff') when the expected written value is known, "
    "or stop at a breakpoint and call dbg_memory_find around the suspected write."
)
WATCHPOINT_MODES = frozenset({"read", "write", "rw"})


def normalize_watchpoint_mode(mode: str) -> str:
    if not isinstance(mode, str):
        raise error("bad_option", "mode must be a string.", "Use read, write or rw.")
    normalized = mode.strip().lower()
    aliases = {"r": "read", "w": "write", "readwrite": "rw", "read-write": "rw"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in WATCHPOINT_MODES:
        raise error("bad_option", f"Invalid watchpoint mode: {mode!r}", "Use read, write or rw.")
    return normalized


def validate_watchpoint_address(address: int) -> int:
    if isinstance(address, bool) or not isinstance(address, int):
        raise error("bad_option", "address must be an integer.", "Pass a decimal or 0x-prefixed integer.")
    if address < 0 or address > 0xFFFFFFFF:
        raise error("bad_option", "address must be between 0 and 0xffffffff.", "Use a 32-bit address.")
    return address


def validate_watchpoint_length(length: int) -> int:
    if isinstance(length, bool) or not isinstance(length, int):
        raise error("bad_option", "length must be an integer.", "Pass a length between 1 and 8 bytes.")
    if length < 1 or length > 8:
        raise error("bad_option", "length must be between 1 and 8.", "Use a byte, word or long watch range.")
    return length


def register(server: MCPServer, app: App) -> None:
    @server.tool(
        name="dbg_watchpoint_set",
        description=(
            "Report whether native watchpoints are available. Stock Hatari does not provide "
            "watchpoints; this tool returns supported=false with the exact limitation and hint."
        ),
        annotations=ToolAnnotations(destructiveHint=False, openWorldHint=False),
    )
    @guarded
    async def dbg_watchpoint_set(
        address: int,
        length: int = 2,
        mode: str = "write",
    ) -> dict[str, Any]:
        start = validate_watchpoint_address(address)
        size = validate_watchpoint_length(length)
        requested_mode = normalize_watchpoint_mode(mode)
        return {
            "ok": True,
            "supported": False,
            "address": start,
            "length": size,
            "mode": requested_mode,
            "limitation": WATCHPOINT_LIMITATION,
            "hint": WATCHPOINT_HINT,
            "warnings": ["No watchpoint state was changed."],
        }

    @server.tool(
        name="dbg_watchpoint_clear",
        description=(
            "Clear watchpoints. Stock Hatari has no watchpoints, so this is an explicit "
            "no-op that returns supported=false rather than pretending to delete one."
        ),
        annotations=ToolAnnotations(destructiveHint=False, openWorldHint=False),
    )
    @guarded
    async def dbg_watchpoint_clear() -> dict[str, Any]:
        return {
            "ok": True,
            "supported": False,
            "cleared": False,
            "limitation": WATCHPOINT_LIMITATION,
            "hint": WATCHPOINT_HINT,
            "warnings": ["No watchpoint state was changed."],
        }
