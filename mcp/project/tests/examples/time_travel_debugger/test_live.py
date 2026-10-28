"""Live smoke test: real EmuTOS, real checkpoints, real state reload."""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[3]
PROGRAMS = PROJECT / "examples" / "programs"
if str(PROGRAMS) not in sys.path:
    sys.path.insert(0, str(PROGRAMS))

from common import paths  # noqa: E402
from common.mcp_client import MCPClient  # noqa: E402
from time_travel_debugger.engine import CheckpointEngine  # noqa: E402


def _hatari() -> Path | None:
    repo = PROJECT.parents[1]
    candidates = [
        repo / "build-install" / "bin" / "hatari",
        repo / "build" / "src" / "hatari",
    ]
    which = shutil.which("hatari")
    if which:
        candidates.append(Path(which))
    return next((item for item in candidates if item and Path(item).is_file()), None)


HATARI = _hatari()
EMUTOS = paths.find_tos("emutos")


@pytest.mark.skipif(
    HATARI is None or EMUTOS is None,
    reason="Hatari or EmuTOS is unavailable; live smoke skipped",
)
def test_live_checkpoints_load_and_reload(tmp_path: Path) -> None:
    state_base = tmp_path / "state"
    client = MCPClient(call_timeout=60.0, shutdown_timeout=30.0)
    engine = None
    try:
        with client:
            engine = CheckpointEngine(
                client,
                state_base=state_base,
                session_id="live-smoke",
                machine="st",
                tos=EMUTOS,
                memory_regions="0x70000:64",
                retention=5,
            )
            status = engine.start_emutos(timeout=90.0)
            assert status.get("state") in {"running", "starting", "at_prompt"}
            engine.wait_until_vbl(50, timeout=45.0)

            first = engine.capture_checkpoint()
            time.sleep(1.2)
            second = engine.capture_checkpoint()

            assert first.id != second.id
            for checkpoint in (first, second):
                assert Path(checkpoint.state_path).is_file()
                assert checkpoint.screenshot_path is not None
                assert Path(checkpoint.screenshot_path).is_file()
                assert checkpoint.registers.get("pc")
                assert checkpoint.memory and len(checkpoint.memory[0].bytes()) == 64
                assert checkpoint.frame_sha256

            inspected = engine.inspect_checkpoint(first.id)
            assert inspected["registers"].get("pc")
            assert inspected["screenshot_path"] == first.screenshot_path
            assert Path(inspected["screenshot_path"]).is_file()
            assert inspected["disassembly"].get("lines") or inspected["disassembly"].get("raw")
            assert inspected["memory"] and inspected["memory"][0]["length"] == 64

            reloaded = CheckpointEngine(
                client,
                state_base=state_base,
                session_id="live-smoke",
                machine="st",
                tos=EMUTOS,
                memory_regions="0x70000:64",
                retention=5,
            )
            assert reloaded.store.load_error is None
            assert [item.id for item in reloaded.recorded()][:2] == [first.id, second.id]
            restored = reloaded.inspect_checkpoint(first.id)
            assert restored["registers"].get("pc")
            assert len(reloaded.recorded()) >= 2

            stopped = engine.stop(force=True)
            assert stopped.get("state") in {"exited", "stopped"} or "error" not in stopped
            engine = None
    finally:
        if engine is not None:
            try:
                engine.stop(force=True)
            except BaseException:
                pass
        shutil.rmtree(state_base, ignore_errors=True)
