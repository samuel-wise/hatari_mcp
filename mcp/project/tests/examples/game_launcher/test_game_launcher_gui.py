"""Headless Tk window/catalog smoke via xvfb-run."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[3]
PROGRAMS = PROJECT / "examples" / "programs"
XVFB = shutil.which("xvfb-run")


def _leftover_smoke_processes() -> list[str]:
    found: list[str] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return found
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        cmdline = raw.replace(b"\0", b" ").decode("utf-8", "replace")
        if "game_launcher.smoke_gui" in cmdline:
            found.append(f"{entry.name}: {cmdline}")
    return found


@pytest.mark.skipif(XVFB is None, reason="xvfb-run is not installed")
def test_headless_window_loads_catalog_and_exits(tmp_path: Path) -> None:
    games = tmp_path / "games"
    games.mkdir()
    for index in range(4):
        (games / f"Headless Game {index} (199{index})(Test)(Action).st").write_bytes(
            b"ST" * 32
        )
    config = tmp_path / "config.json"
    state = tmp_path / "state"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROGRAMS) + os.pathsep + env.get("PYTHONPATH", "")
    command = [
        XVFB,
        "-a",
        sys.executable,
        "-m",
        "game_launcher.smoke_gui",
        "--games",
        str(games),
        "--config",
        str(config),
        "--state",
        str(state),
        "--timeout",
        "8",
        "--min-entries",
        "4",
    ]
    completed = subprocess.run(
        command,
        cwd=PROGRAMS,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert "gui_smoke:" in completed.stdout
    assert "entries=4" in completed.stdout
    assert "ready=True" in completed.stdout
    assert "tree_rows=4" in completed.stdout
    assert _leftover_smoke_processes() == []


@pytest.mark.skipif(XVFB is None, reason="xvfb-run is not installed")
def test_webp_cover_art_renders_without_crashing(tmp_path: Path) -> None:
    pil = pytest.importorskip("PIL.Image")
    games = tmp_path / "games"
    games.mkdir()
    first = games / "Headless Art (1990)(Test)(Action).st"
    first.write_bytes(b"ST" * 32)
    (games / "Headless Plain (1991)(Test)(Action).st").write_bytes(b"ST" * 32)
    cover = games / "cover.webp"
    pil.new("RGB", (640, 360), (30, 60, 90)).save(cover, "WEBP")
    (games / f"{first.name}.json").write_text(
        json.dumps({"art": cover.name}), encoding="utf-8"
    )
    config = tmp_path / "config.json"
    state = tmp_path / "state"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROGRAMS) + os.pathsep + env.get("PYTHONPATH", "")
    completed = subprocess.run(
        [
            XVFB,
            "-a",
            sys.executable,
            "-m",
            "game_launcher.smoke_gui",
            "--games",
            str(games),
            "--config",
            str(config),
            "--state",
            str(state),
            "--timeout",
            "8",
            "--min-entries",
            "2",
        ],
        cwd=PROGRAMS,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert "entries=2" in completed.stdout
    assert "has no attribute 'subsample'" not in completed.stderr
    assert _leftover_smoke_processes() == []
