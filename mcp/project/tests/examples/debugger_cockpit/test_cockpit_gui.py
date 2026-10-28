"""Headless Tk smoke: build the cockpit window under Xvfb and close via after()."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[3]
PROGRAMS = PROJECT / "examples" / "programs"


@pytest.mark.skipif(shutil.which("xvfb-run") is None, reason="xvfb-run is unavailable")
def test_headless_gui_builds_and_closes(tmp_path: Path) -> None:
    code = f"""
import sys
import tkinter as tk
sys.path.insert(0, {str(PROGRAMS)!r})
from debugger_cockpit.app import CockpitApp

root = tk.Tk()
app = CockpitApp(root, state_dir={str(tmp_path / 'state')!r}, auto_poll=False)
root.update_idletasks()
root.update()

def finish():
    app.close()

root.after(300, finish)
root.mainloop()
print('COCKPIT GUI OK')
"""
    env = os.environ.copy()
    env["HATARI_MCP_EXAMPLES_STATE"] = str(tmp_path / "examples-state")
    completed = subprocess.run(
        ["xvfb-run", "-a", sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert "COCKPIT GUI OK" in completed.stdout
    assert (tmp_path / "state").is_dir()
