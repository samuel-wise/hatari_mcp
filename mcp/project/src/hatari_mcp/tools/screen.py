"""Screen capture and video inspection."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..app import App
from ..errors import error
from ._common import guarded


def register(server: MCPServer, app: App) -> None:
    @server.tool(
        name="screen_screenshot",
        description=(
            "Capture the current emulated screen with the debugger screenshot command. "
            "Formats: .png (if built with libpng), .bmp, .neo, .ximg/.img. "
            "Rendering stays enabled because SDL_VIDEODRIVER=dummy is used."
        ),
        annotations=ToolAnnotations(destructiveHint=False, openWorldHint=False),
    )
    @guarded
    async def screen_screenshot(path: str | None = None) -> dict[str, Any]:
        if path:
            target = app.guard_path(path)
        else:
            runtime = app.supervisor.runtime
            if not runtime:
                raise error("not_running", "Hatari has no runtime directory.", "Call emu_start first.")
            target = runtime.path / "screen.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        result = await app.supervisor.debug(f'screenshot "{target}"', timeout_ms=10000)
        exists = target.is_file()
        return {
            **result,
            "path": str(target),
            "exists": exists,
            "size": target.stat().st_size if exists else 0,
        }

    @server.tool(
        name="screen_info",
        description="Return Hatari's Videl/video information as raw text.",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )
    @guarded
    async def screen_info(which: str = "videl") -> dict[str, Any]:
        if which not in {"video", "videl", "vdi"}:
            raise error("bad_option", f"Invalid screen info target: {which}", "Use video, videl or vdi.")
        return await app.supervisor.debug(f"info {which}")
