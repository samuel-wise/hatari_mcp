"""Headless Tk window smoke via xvfb-run."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import PROGRAMS

APP = PROGRAMS / "crash_detective" / "app.py"
XVFB = shutil.which("xvfb-run")


@pytest.mark.skipif(XVFB is None, reason="xvfb-run is unavailable")
def test_gui_builds_and_exits_via_after(tmp_path: Path) -> None:
    state = tmp_path / "state"
    command = [
        XVFB,
        "-a",
        sys.executable,
        str(APP),
        "--state-dir",
        str(state),
        "--no-scan",
        "--autoclose-ms",
        "1200",
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, f"stdout={completed.stdout}\nstderr={completed.stderr}"
    assert state.is_dir()
    # The window must have been built and shut down cleanly.
    assert "Traceback" not in completed.stderr
