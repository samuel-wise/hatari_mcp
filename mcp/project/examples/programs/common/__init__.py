"""Shared foundation for the Hatari MCP Tkinter example programs."""

from __future__ import annotations

from .art import ArtResolver, Artwork, Placeholder, placeholder_for
from .catalog import Catalog, DiskEntry, TosecName, discover, parse_tosec_name, stable_id
from .config import Config, atomic_write_json, load
from .debug_session import DebugSession, DebugSessionError
from .game_runner import GameResult, GameSession, profile_args
from .mcp_client import (
    MCPClient,
    MCPClientError,
    MCPTimeout,
    MCPToolError,
    normalize_tool_result,
)

__all__ = [
    "ArtResolver",
    "Artwork",
    "Catalog",
    "Config",
    "DebugSession",
    "DebugSessionError",
    "DiskEntry",
    "GameResult",
    "GameSession",
    "MCPClient",
    "MCPClientError",
    "MCPTimeout",
    "MCPToolError",
    "Placeholder",
    "TosecName",
    "atomic_write_json",
    "discover",
    "load",
    "normalize_tool_result",
    "parse_tosec_name",
    "placeholder_for",
    "profile_args",
    "stable_id",
]
