"""Live smoke: launch one real zipped TOSEC disk and verify no orphan."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from conftest import PROJECT
from crash_detective import analysis
from crash_detective.controller import CrashDetective
from crash_detective.discovery import make_entry

HATARI_CANDIDATES = [
    PROJECT.parents[1] / "build-install" / "bin" / "hatari",
    PROJECT.parents[1] / "build" / "src" / "hatari",
    Path(shutil.which("hatari")) if shutil.which("hatari") else None,
]
HATARI = next((item for item in HATARI_CANDIDATES if item and Path(item).is_file()), None)

try:
    from common import paths
except ImportError:  # pragma: no cover - path was injected by conftest
    paths = None


def _tos():
    return paths.find_tos("emutos") if paths is not None else None


def _real_zip():
    if paths is None:
        return None
    root = paths.default_game_root()
    if not root.is_dir():
        return None
    preferred = [
        root / "TOSEC-v2011-08-31_CM" / "Automation Menu Disk 270 (1990)(Automation)[STE fixed].zip",
        root / "TOSEC-v2011-08-31_CM" / "Medway Boys Menu Disk 102 (19xx)(Medway Boys)[a].zip",
    ]
    for candidate in preferred:
        if candidate.is_file():
            return candidate, root
    for candidate in sorted(root.rglob("*.zip")):
        return candidate, root
    return None


TOS = _tos()
REAL = _real_zip()


def _pid_alive(pid: int | None) -> bool:
    return bool(pid) and Path(f"/proc/{int(pid)}").exists()


@pytest.mark.skipif(HATARI is None or TOS is None, reason="Hatari or EmuTOS/TOS is unavailable")
@pytest.mark.skipif(REAL is None, reason="local TOSEC corpus is unavailable")
def test_live_disk_launch_capture_and_cleanup(tmp_path: Path) -> None:
    archive, root = REAL
    entry = make_entry(archive, root)
    state = tmp_path / "state"
    detective = CrashDetective(state_dir=state, machine="st", tos=TOS, timeout=35.0)
    try:
        payload = detective.launch_disk(entry, machine="st", tos=TOS, timeout=35.0)
    finally:
        detective.close()

    record = payload["record"]
    result = record["result"]
    classification = payload["classification"]
    assert result["ok"] in (True, False)
    assert classification["category"]
    after = record.get("status_after_stop") or {}
    assert after.get("state") in {"stopped", "exited", "crashed", "failed"}
    assert record["frames"], "expected at least one captured frame"
    assert record["logs"]["stderr"], "expected a Hatari stderr tail"

    bundle = Path(payload["bundle"])
    assert bundle.is_dir(), bundle
    assert (bundle / "manifest.json").is_file()
    verification = analysis.verify_bundle(bundle)
    assert verification["ok"], verification["errors"]
    assert (bundle / "report.md").is_file()
    assert (bundle / "report.json").is_file()
    assert list((bundle / "screenshots").glob("*")), "expected bundle screenshots"

    pid = record["status"].get("pid")
    assert pid, "expected the MCP status to report the Hatari PID"
    assert not _pid_alive(pid), f"Hatari PID {pid} is still alive after stop/close"


def test_live_smoke_helpers_are_consistent() -> None:
    # These assertions document the skip inputs without starting an emulator.
    if HATARI is not None:
        assert HATARI.is_file()
    if TOS is not None:
        assert TOS.is_file()
    if REAL is not None:
        archive, _root = REAL
        assert archive.suffix == ".zip"
