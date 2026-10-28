"""Live smoke on real zipped TOSEC disks.

Starts one direct-boot game disk, hot-swaps to a second disk on the same
emulator process, stops, and verifies no Hatari child is left behind.  Skips
cleanly when the corpus, EmuTOS/TOS or Hatari is unavailable.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
import zipfile
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[3]
PROGRAMS = PROJECT / "examples" / "programs"
if str(PROGRAMS) not in sys.path:
    sys.path.insert(0, str(PROGRAMS))

from common import Config, paths  # noqa: E402
from common.catalog import DiskEntry  # noqa: E402
from game_launcher.metadata import MetadataResolver  # noqa: E402
from game_launcher.profiles import discover_tos_candidates  # noqa: E402
from game_launcher.service import LauncherService  # noqa: E402

GAME_ROOT = Path(paths.DEFAULT_GAME_ROOT)
PREFERRED = (
    "International Karate (1986)(System 3)[cr CSS].zip",
    "Speedball (1988)(Image Works)[a2].zip",
    "Buggy Boy (1988)(Elite)[t Noid].zip",
)


def _hatari_binary() -> Path | None:
    repo = PROJECT.parents[1]
    candidates = [
        repo / "build-install" / "bin" / "hatari",
        repo / "build" / "src" / "hatari",
        Path(shutil.which("hatari")) if shutil.which("hatari") else None,
    ]
    return next((item for item in candidates if item and Path(item).is_file()), None)


def _has_supported_disk(archive: Path) -> bool:
    try:
        with zipfile.ZipFile(archive) as handle:
            return any(
                Path(name).suffix.lower() in {".st", ".msa", ".stx", ".dim", ".ipf"}
                for name in handle.namelist()
                if not name.startswith("__MACOSX/")
            )
    except (OSError, zipfile.BadZipFile):
        return False


def _pick_disks(limit: int = 2) -> list[Path]:
    if not GAME_ROOT.is_dir():
        return []
    found: list[Path] = []
    for name in PREFERRED:
        for match in GAME_ROOT.rglob("*.zip"):
            if match.name != name:
                continue
            if _has_supported_disk(match):
                found.append(match)
                break
        if len(found) >= limit:
            return found[:limit]
    if len(found) < limit:
        for archive in sorted(GAME_ROOT.rglob("*.zip")):
            if archive in found:
                continue
            lowered = archive.name.casefold()
            if any(tag in lowered for tag in ("[ste", "[tt", "[falcon", "aga")):
                continue
            if _has_supported_disk(archive):
                found.append(archive)
            if len(found) >= limit:
                break
    return found[:limit]


def _hatari_pids() -> set[int]:
    pids: set[int] = set()
    proc = Path("/proc")
    if not proc.is_dir():
        return pids
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == os.getpid():
            continue
        try:
            parts = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        for part in parts[:3]:
            if Path(part.decode("utf-8", "replace")).name == "hatari":
                pids.add(pid)
                break
    return pids


HATARI = _hatari_binary()
TOS = discover_tos_candidates().get("emutos")
DISKS = _pick_disks()

pytestmark = pytest.mark.skipif(
    HATARI is None or TOS is None or len(DISKS) < 2,
    reason="Hatari, EmuTOS/TOS or two real zipped corpus disks are unavailable",
)


def test_live_start_swap_stop_and_no_orphan(tmp_path: Path) -> None:
    config = Config(tmp_path / "state" / "config.json")
    metadata = MetadataResolver(state_dir=tmp_path / "state")
    service = LauncherService(
        config=config,
        state_dir=tmp_path / "state",
        metadata=metadata,
        default_machine="st",
        boot_timeout=60.0,
    )
    before = _hatari_pids()
    our_pids: set[int] = set()
    try:
        first = DiskEntry.from_path(DISKS[0], GAME_ROOT)
        second = DiskEntry.from_path(DISKS[1], GAME_ROOT)
        outcome_one = service.launch(first, requested_machine="st", timeout=60.0)
        assert outcome_one.result.ok, (
            f"{DISKS[0]} failed: {outcome_one.result.failure_reason} "
            f"{outcome_one.result.message}\n{outcome_one.evidence_path}"
        )
        assert outcome_one.action == "start"
        assert outcome_one.result.frame_path
        assert Path(outcome_one.result.frame_path).is_file()
        assert outcome_one.evidence_path and Path(outcome_one.evidence_path).is_file()
        status_one = service.status()
        pid_one = status_one.get("pid")
        if pid_one:
            our_pids.add(int(pid_one))

        outcome_two = service.launch(second, requested_machine="st", timeout=60.0)
        assert outcome_two.result.ok, (
            f"{DISKS[1]} failed: {outcome_two.result.failure_reason} "
            f"{outcome_two.result.message}\n{outcome_two.evidence_path}"
        )
        assert outcome_two.action == "swap", outcome_two.explanation
        assert outcome_two.evidence_path and Path(outcome_two.evidence_path).is_file()
        status_two = service.status()
        pid_two = status_two.get("pid")
        if pid_two:
            our_pids.add(int(pid_two))
        if pid_one and pid_two:
            assert pid_one == pid_two, "swap unexpectedly replaced the Hatari process"
        assert our_pids, "could not identify the Hatari process for the orphan check"

        record = config.get_game(first.id)
        assert record.get("last_success_config")
        assert record.get("launcher", {}).get("evidence_path")
        failure_record = config.get_game(second.id)
        assert failure_record.get("last_result", {}).get("ok") is True

        stopped = service.stop()
        assert stopped.get("state") in {"exited", "stopped", "stopping"}
    finally:
        service.close()
        time.sleep(0.5)
    after = _hatari_pids()
    leaked = our_pids & after
    assert not leaked, f"orphan Hatari processes left behind: {leaked}"
    # Other tests may start/stop emulators concurrently; only this session's
    # process identity is attributable to this test.
    assert our_pids - before, "the test never observed its own Hatari process"
