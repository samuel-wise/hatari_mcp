"""Log and console readers."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..app import App
from ._common import guarded


def register(server: MCPServer, app: App) -> None:
    @server.tool(
        name="log_read",
        description=(
            "Read classified Hatari/debugger output from stderr. "
            "Use the returned cursor as since= for incremental reads."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )
    @guarded
    async def log_read(
        lines: int = 200,
        since: int | None = None,
        kind: str | None = None,
    ) -> dict[str, Any]:
        kinds = {kind} if kind else None
        entries, cursor = app.supervisor.stderr_ring.read(since=since, limit=lines, kinds=kinds)
        return {
            "ok": True,
            "entries": [entry.as_dict() for entry in entries],
            "cursor": cursor,
        }

    @server.tool(
        name="console_read",
        description=(
            "Read TOS console output (--conout 2) from stdout. "
            "Use the returned cursor as since= for incremental reads."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )
    @guarded
    async def console_read(lines: int = 200, since: int | None = None) -> dict[str, Any]:
        entries, cursor = app.supervisor.stdout_ring.read(since=since, limit=lines)
        return {
            "ok": True,
            "entries": [entry.as_dict() for entry in entries],
            "cursor": cursor,
        }
