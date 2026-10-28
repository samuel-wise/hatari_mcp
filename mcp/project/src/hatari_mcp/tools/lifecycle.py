"""Lifecycle tools."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..app import App
from ..errors import error
from ._common import guarded, readonly


def register(server: MCPServer, app: App) -> None:
    @server.tool(
        name="emu_start",
        description=(
            "Start one headless Hatari instance with a private control socket. "
            "Optionally map a host directory as a GEMDOS drive and autostart a program. "
            "Returns immediately after Hatari connects; boot may still be in progress. "
            "NatFeats is off by default because NF_STDERR lets the guest forge diagnostic text; "
            "enable natfeats=true only for cooperative programs that need NF_EXIT."
        ),
        annotations=ToolAnnotations(destructiveHint=False, openWorldHint=False),
    )
    @guarded
    async def emu_start(
        profile: str | None = None,
        args: list[str] | None = None,
        tos: str | None = None,
        display: bool = False,
        workspace: str | None = None,
        autostart: str | None = None,
        gemdos_drive: str = "C",
        gemdos_case: str = "off",
        gemdos_time: str = "atari",
        gemdos_conv: str = "off",
        write_protect: str = "off",
        natfeats: bool = False,
    ) -> dict[str, Any]:
        return await app.start(
            profile=profile,
            args=args,
            tos=tos,
            display=display,
            workspace=workspace,
            autostart=autostart,
            gemdos_drive=gemdos_drive,
            gemdos_case=gemdos_case,
            gemdos_time=gemdos_time,
            gemdos_conv=gemdos_conv,
            write_protect=write_protect,
            natfeats=natfeats,
        )

    @server.tool(
        name="emu_stop",
        description="Stop Hatari with a graceful quit, then SIGTERM/SIGKILL if needed. Idempotent.",
        annotations=ToolAnnotations(destructiveHint=True, idempotentHint=True, openWorldHint=False),
    )
    @guarded
    async def emu_stop(force: bool = False) -> dict[str, Any]:
        return await app.supervisor.stop(force=force)

    @server.tool(
        name="emu_status",
        description="Return lifecycle, process, socket, exit and debugger-prompt state.",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
    )
    @guarded
    async def emu_status() -> dict[str, Any]:
        return app.status()

    @server.tool(
        name="emu_pause",
        description=(
            "Pause the emulation by sending SIGSTOP to the Hatari child. "
            "Debugger/control-socket commands are unavailable while paused; call emu_resume first. "
            "Pausing is only supported from the running state, not at the debugger prompt."
        ),
        annotations=ToolAnnotations(destructiveHint=False, idempotentHint=True),
    )
    @guarded
    async def emu_pause() -> dict[str, Any]:
        return await app.supervisor.pause()

    @server.tool(
        name="emu_resume",
        description=(
            "Resume a SIGSTOP-paused emulator with SIGCONT and wait for a control-socket "
            "acknowledgement before reporting running. At the debugger prompt this sends 'cont' on stdin."
        ),
        annotations=ToolAnnotations(destructiveHint=False, idempotentHint=True),
    )
    @guarded
    async def emu_resume() -> dict[str, Any]:
        return await app.supervisor.resume()

    @server.tool(
        name="emu_wait_for_exit",
        description=(
            "Wait for Hatari to exit or the timeout to expire. "
            "NatFeats NF_EXIT is detected only when emu_start was called with natfeats=true; "
            "Pterm auto-exit detection is not enabled by default."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )
    @guarded
    async def emu_wait_for_exit(timeout_ms: int = 30000) -> dict[str, Any]:
        return await app.supervisor.wait_for_exit(timeout_ms)

    @server.tool(
        name="emu_reset",
        description="Reset the emulated machine. kind=warm maps to soft, cold maps to hard.",
        annotations=ToolAnnotations(destructiveHint=True),
    )
    @guarded
    async def emu_reset(kind: str = "hard") -> dict[str, Any]:
        mapping = {"warm": "soft", "cold": "hard", "soft": "soft", "hard": "hard"}
        native = mapping.get(kind.lower())
        if not native:
            raise error("bad_option", f"Invalid reset kind: {kind}", "Use warm, cold, soft or hard.")
        result = await app.supervisor.debug(f"reset {native}", channel="auto")
        return {**result, "kind": kind, "native": native}

    @server.tool(
        name="emu_quit_debugger",
        description="Leave/quit the debugger. At the prompt this uses stdin, never the queued socket.",
        annotations=ToolAnnotations(destructiveHint=True),
    )
    @guarded
    async def emu_quit_debugger() -> dict[str, Any]:
        return await app.supervisor.debug("quit", channel="auto")
