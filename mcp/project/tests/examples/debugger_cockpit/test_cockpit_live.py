"""Live EmuTOS smoke for the debugger cockpit workflow."""

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
from common.debug_session import DebugSession  # noqa: E402
from common.mcp_client import MCPClient  # noqa: E402


def _hatari_and_tos() -> tuple[Path | None, Path | None]:
    repo = PROJECT.parents[1]
    candidates = [
        repo / "build-install" / "bin" / "hatari",
        repo / "build" / "src" / "hatari",
        Path(shutil.which("hatari")) if shutil.which("hatari") else None,
    ]
    hatari = next((item for item in candidates if item and Path(item).is_file()), None)
    return hatari, paths.find_tos("emutos")


HATARI, EMUTOS = _hatari_and_tos()
pytestmark = pytest.mark.skipif(
    HATARI is None or EMUTOS is None, reason="Hatari or EmuTOS is unavailable"
)


def _read_stat(pid: int) -> tuple[int, int] | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    close = raw.rfind(")")
    if close < 0:
        return None
    fields = raw[close + 2 :].split()
    try:
        return int(fields[1]), int(fields[2])
    except (IndexError, ValueError):
        return None


def _descendants(pid: int) -> set[int]:
    if not Path("/proc").is_dir():
        return set()
    found: set[int] = set()
    pending = [pid]
    while pending:
        current = pending.pop()
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            child = int(entry.name)
            if child in found:
                continue
            stat = _read_stat(child)
            if stat and stat[0] == current:
                found.add(child)
                pending.append(child)
    return found


def _alive(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


def test_live_emutos_prompt_breakpoint_step_continue_stop(tmp_path: Path) -> None:
    client = MCPClient(call_timeout=60.0, shutdown_timeout=20.0)
    client.start()
    server_pid = client._server_pid
    session = DebugSession(client, state_dir=tmp_path / "state", poll_interval=0.15)
    descendants: set[int] = set()
    try:
        status = session.start_emutos(tos=EMUTOS, timeout=45.0)
        assert status["state"] in {"running", "at_prompt"}
        assert session.status()["state"] != "stopped"

        status = session.ensure_prompt(timeout=12.0)
        assert status.get("at_prompt") is True

        registers = session.registers().get("registers") or {}
        assert isinstance(registers.get("pc"), int)
        pc = int(registers["pc"])

        set_result = session.breakpoint_set(pc + 2, once=True, quiet=False)
        assert set_result.get("ok") is not False
        listed = session.breakpoint_list()
        assert "breakpoint" in str(listed.get("raw", "")).lower()

        session.continue_run()
        time.sleep(0.2)
        status = session.wait_prompt(timeout=10.0)
        assert status.get("at_prompt") is True
        stopped_pc = (session.registers().get("registers") or {}).get("pc")
        assert isinstance(stopped_pc, int) and stopped_pc >= pc, (
            f"one-shot breakpoint stop has an unexpected PC: {stopped_pc} (started {pc})"
        )

        stepped = session.step()
        assert stepped.get("ok") is not False
        assert session.status().get("at_prompt") is True

        session.continue_run()
        time.sleep(0.2)
        assert session.status().get("at_prompt") is False or session.status().get("state") == "running"

        descendants = _descendants(server_pid) if server_pid else set()
        stopped = session.stop()
        assert stopped.get("state") in {"exited", "stopped"}
    finally:
        try:
            session.stop(force=True)
        except BaseException:
            pass
        client.stop()

    if server_pid:
        assert not _alive(server_pid), f"MCP server {server_pid} is still alive"
    for pid in descendants:
        assert not _alive(pid), f"orphan process {pid} survived shutdown"


def test_live_sample_build_and_crash_path(tmp_path: Path) -> None:
    """Compile the bundled sample through MCP AHCC and exercise its crash path."""

    from debugger_cockpit.controller import CockpitController

    controller = CockpitController(state_dir=tmp_path / "cockpit", tos=EMUTOS)
    try:
        controller.connect()
        built = controller.build_sample()
        assert built.get("ok") is not False, built
        output = Path(built["output"])
        assert output.is_file() and output.stat().st_size > 1000

        status = controller.load_prg(output)
        assert status["state"] in {"running", "at_prompt"}

        text = "\n".join(controller.console_tail(200))
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and "COCKPIT SAMPLE READY" not in text:
            time.sleep(0.25)
            text = "\n".join(controller.console_tail(200))
        assert "COCKPIT SAMPLE READY" in text

        status = controller.enter_debugger()
        assert status.get("at_prompt") is True
        go = __import__("re").search(r"GO_FLAG=\$([0-9a-fA-F]+)", text)
        crash = __import__("re").search(r"CRASH_FLAG=\$([0-9a-fA-F]+)", text)
        assert go and crash
        controller.write_memory(int(go.group(1), 16), "c0de")
        controller.write_memory(int(crash.group(1), 16), "0001")
        controller.continue_run()

        deadline = time.monotonic() + 30.0
        final = controller.status()
        while time.monotonic() < deadline:
            final = controller.status()
            if final.get("exception"):
                break
            time.sleep(0.2)
        assert final.get("exception"), f"sample did not raise its deliberate exception: {final}"
        assert controller.session is not None
        exception = controller.session.exception()
        assert exception.get("has_exception") is True

        bundle = controller.capture_triage("sample deliberate exception")
        assert Path(bundle["path"]).is_dir()
        assert (Path(bundle["path"]) / "report.md").is_file()
        assert "Illegal instruction" in bundle["report"]
        assert "screenshot" in bundle["report"].lower()
    finally:
        controller.close()
