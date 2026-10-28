"""Headless Tk smoke: build every widget, then exit with after()."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[3]
PROGRAMS = PROJECT / "examples" / "programs"
if str(PROGRAMS) not in sys.path:
    sys.path.insert(0, str(PROGRAMS))


def test_gui_builds_and_exits_via_after() -> None:
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        pytest.skip("no display; run with xvfb-run -a")
    tk = pytest.importorskip("tkinter")
    from time_travel_debugger.app import TimeTravelApp

    root = tk.Tk()
    app = TimeTravelApp(root, engine=None, auto_connect=False)

    def finish() -> None:
        app.close()
        root.destroy()

    root.after(100, finish)
    root.after(5000, root.destroy)
    root.mainloop()
    assert app.connected is False
