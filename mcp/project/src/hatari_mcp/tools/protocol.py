"""Protocol capability reporting."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .. import __version__
from ..app import App
from ..errors import error
from ..protocol.capabilities import CANONICAL_CAPABILITIES
from ..protocol.hello import find_banner
from ..protocol.selector import mode_from_env, parse_mode, selection_for_status
from ._common import guarded, readonly


def capability_report(app: App, *, mode: str | None = None) -> dict[str, Any]:
    supervisor = app.supervisor
    banner = supervisor.protocol_banner
    if banner is None and supervisor.protocol_banner_raw:
        banner = find_banner([supervisor.protocol_banner_raw])
    if mode is None:
        requested = mode_from_env()
    else:
        requested = parse_mode(mode)
    report = selection_for_status(
        env={"HATARI_MCP_PROTOCOL": requested.value},
        banner=banner,
        banner_error=getattr(supervisor, "protocol_banner_error", None),
    )
    report["server_version"] = __version__
    report["selection"] = {
        "requested_mode": requested.value,
        "protocol_mode": report.get("protocol_mode"),
        "banner_seen": banner is not None,
        "banner": supervisor.protocol_banner_raw,
        "banner_error": getattr(supervisor, "protocol_banner_error", None),
        "handshake_complete": False,
    }
    return report


def register(server: MCPServer, app: App) -> None:
    @server.tool(
        name="emu_capabilities",
        description=(
            "Report the selected control protocol and the full capability matrix. "
            "Stock Hatari reports legacy truthfully: framed/binary/frame-step features are "
            "listed as fallback or unsupported instead of being silently emulated."
        ),
        annotations=ToolAnnotations(**readonly()),
    )
    @guarded
    async def emu_capabilities(mode: str | None = None) -> dict[str, Any]:
        if mode is not None and mode not in {"auto", "legacy", "framed", "require-framed"}:
            raise error(
                "bad_option",
                f"Invalid capability mode: {mode!r}",
                "Use auto, legacy, framed or require-framed.",
            )
        result = capability_report(app, mode=mode)
        result["canonical_capabilities"] = list(CANONICAL_CAPABILITIES)
        return result
