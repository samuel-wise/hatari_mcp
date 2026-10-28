"""Machine configuration, paths, devices and shortcuts."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..app import App
from ..errors import error
from ._common import guarded

PATH_NAMES = frozenset(
    {
        "memauto",
        "memsave",
        "midiin",
        "midiout",
        "printout",
        "soundout",
        "rs232in",
        "rs232out",
        "sccain",
        "sccaout",
        "sccalanin",
        "sccalanout",
        "sccbin",
        "sccbout",
    }
)
DEVICE_NAMES = frozenset({"printer", "rs232", "scca", "sccalan", "sccb", "midi"})
DEVICE_ACTIONS = frozenset({"enable", "disable", "toggle"})
SHORTCUT_NAMES = frozenset(
    {
        "mousegrab",
        "coldreset",
        "warmreset",
        "screenshot",
        "bosskey",
        "recanim",
        "recsound",
        "savemem",
        "quit",
    }
)


def _choice(value: str, allowed: frozenset[str], kind: str, hint: str) -> str:
    if not isinstance(value, str):
        raise error("bad_option", f"{kind} must be a string.", hint)
    normalized = value.strip().lower()
    if normalized not in allowed:
        raise error("bad_option", f"Unknown {kind}: {value!r}", hint)
    return normalized


def validate_path_name(name: str) -> str:
    return _choice(
        name,
        PATH_NAMES,
        "path name",
        "Use one of: " + ", ".join(sorted(PATH_NAMES)) + ".",
    )


def validate_device(name: str) -> str:
    return _choice(
        name,
        DEVICE_NAMES,
        "device",
        "Use one of: " + ", ".join(sorted(DEVICE_NAMES)) + ".",
    )


def validate_device_action(action: str) -> str:
    return _choice(
        action,
        DEVICE_ACTIONS,
        "device action",
        "Use enable, disable or toggle.",
    )


def validate_shortcut(name: str) -> str:
    return _choice(
        name,
        SHORTCUT_NAMES,
        "shortcut",
        "Use one of: " + ", ".join(sorted(SHORTCUT_NAMES)) + ".",
    )


def validate_path_value(path: str) -> str:
    if not isinstance(path, str):
        raise error("bad_option", "Path must be a string.", "Pass a filesystem path.")
    value = path.strip()
    if not value:
        raise error("bad_option", "Path must not be empty.", "Pass a filesystem path.")
    if any(char in value for char in ("\x00", "\n", "\r")) or any(ord(char) < 32 for char in value):
        raise error(
            "bad_option",
            "Path contains a NUL or control character.",
            "Pass a single-line filesystem path.",
        )
    return value


def _require_control_channel(app: App) -> None:
    state = app.supervisor.state.value
    if state == "at_prompt":
        raise error(
            "not_stopped",
            "Hatari is at the debugger prompt, where control-socket commands queue.",
            "Continue or quit the debugger before changing runtime machine settings.",
        )
    if state not in {"running", "remote_paused"}:
        raise error("not_running", f"Hatari is {state}.", "Call emu_start first.")


async def _send_control(app: App, command: str) -> None:
    _require_control_channel(app)
    await app.supervisor._socket_command(command)


def register(server: MCPServer, app: App) -> None:
    @server.tool(
        name="emu_set_option",
        description=(
            "Apply raw Hatari command-line options over the control socket. "
            "Options that need a restart (machine, memory size, TOS, disks/ACSI/IDE, CPU) "
            "may open a modal reset prompt or cold-reset Hatari (especially after media tools "
            "set --alert-level fatal). The MCP server itself never restarts."
        ),
        annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False),
    )
    @guarded
    async def emu_set_option(options: str) -> dict[str, Any]:
        return await app.set_option(options)

    @server.tool(
        name="emu_set_path",
        description=(
            "Set a Hatari output/input path with hatari-path. Allowed names: "
            "memauto, memsave, midiin, midiout, printout, soundout, rs232in, rs232out, "
            "sccain, sccaout, sccalanin, sccalanout, sccbin, sccbout."
        ),
        annotations=ToolAnnotations(destructiveHint=False, openWorldHint=False),
    )
    @guarded
    async def emu_set_path(name: str, path: str) -> dict[str, Any]:
        path_name = validate_path_name(name)
        path_value = validate_path_value(path)
        await _send_control(app, f"hatari-path {path_name} {path_value}")
        return {
            "ok": True,
            "name": path_name,
            "path": path_value,
            "channel": "socket",
            "applied": "unknown",
        }

    @server.tool(
        name="emu_device",
        description=(
            "Enable, disable or toggle a runtime device with hatari-enable/disable/toggle. "
            "Allowed devices: printer, rs232, scca, sccalan, sccb, midi."
        ),
        annotations=ToolAnnotations(destructiveHint=False, idempotentHint=False),
    )
    @guarded
    async def emu_device(name: str, action: str = "toggle") -> dict[str, Any]:
        device = validate_device(name)
        device_action = validate_device_action(action)
        await _send_control(app, f"hatari-{device_action} {device}")
        return {
            "ok": True,
            "device": device,
            "action": device_action,
            "channel": "socket",
            "applied": "unknown",
        }

    @server.tool(
        name="emu_shortcut",
        description=(
            "Invoke a Hatari shortcut by name. Allowed names: mousegrab, coldreset, "
            "warmreset, screenshot, bosskey, recanim, recsound, savemem, quit."
        ),
        annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False),
    )
    @guarded
    async def emu_shortcut(name: str) -> dict[str, Any]:
        shortcut = validate_shortcut(name)
        await _send_control(app, f"hatari-shortcut {shortcut}")
        return {
            "ok": True,
            "shortcut": shortcut,
            "channel": "socket",
            "applied": "unknown",
        }
