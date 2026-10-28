"""Interactive Hatari debugger cockpit (example program 2a)."""

from __future__ import annotations

__all__ = ["CockpitApp", "CockpitController"]


def __getattr__(name: str):
    if name == "CockpitApp":
        from .app import CockpitApp

        return CockpitApp
    if name == "CockpitController":
        from .controller import CockpitController

        return CockpitController
    raise AttributeError(name)
