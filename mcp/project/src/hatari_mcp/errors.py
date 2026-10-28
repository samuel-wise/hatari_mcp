"""Typed errors shared by every tool and the process supervisor."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class HatariError(Exception):
    code: str
    message: str
    hint: str = ""
    detail: str | None = None
    raw: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        text = f"{self.code}: {self.message}"
        if self.hint:
            text += f" Hint: {self.hint}"
        return text

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": self.code,
            "message": self.message,
            "hint": self.hint,
            "detail": self.detail,
            "raw": self.raw,
            **self.extra,
        }


def error(code: str, message: str, hint: str = "", **kwargs: Any) -> HatariError:
    return HatariError(code=code, message=message, hint=hint, **kwargs)
