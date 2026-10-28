"""Live EmuTOS checks for the crash-safe supervisor and legacy breakpoint flow."""

from __future__ import annotations

import asyncio
import os
import signal
import time

import pytest

from hatari_mcp.config import Settings
from hatari_mcp.errors import HatariError
from hatari_mcp.state import State
from hatari_mcp.supervisor import Supervisor
from hatari_mcp.tools.debug_extra import parse_pc

SETTINGS = Settings.load()
PREREQS = SETTINGS.hatari_bin is not None and SETTINGS.default_tos is not None
pytestmark = pytest.mark.skipif(not PREREQS, reason="Hatari binary or TOS image is unavailable")


def test_live_start_stop_and_unresponsive() -> None:
    async def main() -> None:
        supervisor = Supervisor(
            SETTINGS,
            heartbeat_interval=0.1,
            heartbeat_probe_interval=0.2,
            heartbeat_timeout=0.3,
            heartbeat_misses=3,
        )
        try:
            started = await supervisor.start(startup_timeout=20)
            assert started["state"] == "running"
            assert supervisor.runtime is not None
            assert supervisor.runtime.socket_path.exists()
            status = supervisor.status()
            assert status["capabilities"]["protocol_mode"] == "legacy"
            assert status["capabilities"]["capabilities"]["framed"] is False
            assert status["pid"] and supervisor.proc is not None

            if os.name == "posix":
                os.kill(supervisor.proc.pid, signal.SIGSTOP)
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline and supervisor.state is not State.UNRESPONSIVE:
                    await asyncio.sleep(0.05)
                assert supervisor.state is State.UNRESPONSIVE
                assert supervisor.status()["unresponsive"] is True
                os.kill(supervisor.proc.pid, signal.SIGCONT)

            stopped = await supervisor.stop(force=True)
            assert stopped["state"] == "exited"
            assert supervisor.runtime is None
        finally:
            await supervisor.shutdown()

    asyncio.run(main())


def test_live_breakpoint_prompt_step_continue() -> None:
    async def main() -> None:
        supervisor = Supervisor(
            SETTINGS,
            heartbeat_interval=0.2,
            heartbeat_probe_interval=0.5,
            heartbeat_timeout=0.5,
            heartbeat_misses=3,
        )
        try:
            await supervisor.start(startup_timeout=20)
            await asyncio.sleep(0.5)
            registers = await supervisor.debug("cpureg", channel="socket", timeout_ms=5000)
            pc = parse_pc(registers["raw"])
            assert pc is not None
            await supervisor.debug(
                f"breakpoint pc = ${pc + 2:x} :once", channel="socket", timeout_ms=5000
            )
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and supervisor.state is not State.AT_PROMPT:
                await asyncio.sleep(0.05)
            assert supervisor.state is State.AT_PROMPT
            # stdin channel while the debugger prompt is active
            at_prompt = await supervisor.debug("cpureg")
            assert at_prompt["channel"] == "stdin"
            assigned = await supervisor.debug("cpureg d0=0x1234", channel="stdin")
            assert assigned["channel"] == "stdin"
            stepped = await supervisor.debug("step")
            assert stepped["channel"] == "stdin"
            assert supervisor.state is State.AT_PROMPT
            resumed = await supervisor.resume()
            assert resumed["state"] == "running"
            stopped = await supervisor.stop()
            assert stopped["state"] == "exited"
            assert stopped["returncode"] == 0
            assert stopped["exit_cause"] == "requested_stop"
        finally:
            await supervisor.stop(force=True)
            await supervisor.shutdown()

    asyncio.run(main())


def test_live_strict_mode_refuses_stock_hatari(monkeypatch) -> None:
    monkeypatch.setenv("HATARI_MCP_PROTOCOL", "framed")

    async def main() -> None:
        supervisor = Supervisor(SETTINGS, heartbeat_interval=0.2, heartbeat_probe_interval=0.5)
        try:
            with pytest.raises(HatariError) as excinfo:
                await supervisor.start(startup_timeout=20)
            assert excinfo.value.code == "protocol_unavailable"
            assert supervisor.protocol_banner is None
            assert supervisor.runtime is None
        finally:
            await supervisor.shutdown()

    asyncio.run(main())


def test_live_pause_resume_uses_sigstop_sigcont_and_recovers_socket() -> None:
    if os.name != "posix":
        pytest.skip("remote pause uses POSIX signals")

    async def main() -> None:
        supervisor = Supervisor(
            SETTINGS,
            heartbeat_interval=0.1,
            heartbeat_probe_interval=0.2,
            heartbeat_timeout=0.3,
            heartbeat_misses=3,
        )
        try:
            started = await supervisor.start(startup_timeout=20)
            assert started["state"] == "running"
            await asyncio.sleep(0.5)
            await supervisor.debug("cpureg", channel="socket", timeout_ms=5000)

            paused = await supervisor.pause()
            assert paused["state"] == "remote_paused"
            assert paused["paused"] is True

            # A SIGSTOP'd child cannot service the socket; longer than several
            # heartbeat intervals it must still report remote_paused, not
            # unresponsive.
            await asyncio.sleep(1.0)
            during = supervisor.status()
            assert during["state"] == "remote_paused"
            assert during["unresponsive"] is False
            with pytest.raises(HatariError):
                await supervisor.debug("cpureg", channel="socket", timeout_ms=500)

            resumed = await supervisor.resume()
            assert resumed["state"] == "running"
            regs = await supervisor.debug("cpureg", channel="socket", timeout_ms=5000)
            assert regs["state"] == "running"

            stopped = await supervisor.stop()
            assert stopped["state"] == "exited"
            assert stopped["returncode"] == 0
        finally:
            await supervisor.shutdown()

    asyncio.run(main())
