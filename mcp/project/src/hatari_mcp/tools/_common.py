"""Shared tool helpers."""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from mcp.server.mcpserver.exceptions import ToolError

from ..errors import HatariError

logger = logging.getLogger("hatari_mcp")
T = TypeVar("T")


def guarded(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> T:
        try:
            return await fn(*args, **kwargs)
        except HatariError as exc:
            raise ToolError(str(exc)) from exc
        except ToolError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Unhandled tool failure")
            raise ToolError(f"internal_error: {exc}") from exc

    return wrapper


def readonly(**kwargs: Any) -> dict[str, Any]:
    return {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False, **kwargs}
