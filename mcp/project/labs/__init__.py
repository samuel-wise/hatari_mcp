"""Non-shipping research harnesses for the P8 stretch spikes.

Importing this package is intentionally inert. Spike modules require the
``HATARI_MCP_LABS=1`` environment variable and are never imported by the
production server, registered as MCP tools, or included in the wheel because
this directory lives outside ``src/``.
"""

from __future__ import annotations

import os

LABS_ENV = "HATARI_MCP_LABS"
GUARD_MESSAGE = (
    "P8 lab code is non-shipping research. Set HATARI_MCP_LABS=1 to import "
    "labs submodules; nothing here is registered as an MCP tool."
)


def labs_enabled() -> bool:
    return os.environ.get(LABS_ENV) == "1"


def require_labs() -> None:
    if not labs_enabled():
        raise RuntimeError(GUARD_MESSAGE)


def describe() -> dict[str, object]:
    return {
        "enabled": labs_enabled(),
        "env": LABS_ENV,
        "shipping": False,
        "registered_mcp_tools": 0,
    }


__all__ = ["LABS_ENV", "GUARD_MESSAGE", "describe", "labs_enabled", "require_labs"]
