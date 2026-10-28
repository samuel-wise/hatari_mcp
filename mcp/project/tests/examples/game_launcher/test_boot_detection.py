"""Boot classification tests for GameSession._wait_for_game.

These use a scripted session so no emulator is started.  The real failure mode
this guards against is accepting the EmuTOS boot screen as a game frame: the
boot screen is mostly non-black, so a non-black ratio alone is not enough.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parents[3]
PROGRAMS = PROJECT / "examples" / "programs"
if str(PROGRAMS) not in sys.path:
    sys.path.insert(0, str(PROGRAMS))

from common.game_runner import GameSession  # noqa: E402

BOOT_DIGEST = "a" * 64
GAME_DIGEST = "b" * 64
TOS_PC = 0xFC660E
RAM_PC = 0x0001382A


class ScriptedSession(GameSession):
    def __init__(self, script: list[dict[str, Any]], **kwargs: Any) -> None:
        kwargs.setdefault("poll_interval", 0.05)
        kwargs.setdefault("min_visible_seconds", 1.0)
        super().__init__(None, **kwargs)  # type: ignore[arg-type]
        self.script = script
        self.calls = 0
        self.last_item = script[0]
        self.status = {"state": "running"}

    def _status(self) -> dict[str, Any]:
        return self.status

    def _tails(self) -> dict[str, Any]:
        return {}

    def _capture(self, game_id: str):
        item = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        self.last_item = item
        stats = {
            "width": 320,
            "height": 200,
            "nonblack_ratio": float(item.get("ratio", 0.99)),
            "content_nonblack_ratio": float(item.get("content_ratio", item.get("ratio", 0.99))),
            "unique_colors": int(item.get("colors", 8)),
        }
        return ({"sha256": item["digest"]}, f"/tmp/frame-{self.calls}.bmp", stats, None)

    def _pc(self) -> int | None:
        return self.last_item.get("pc")


class SequencedStatusSession(ScriptedSession):
    def __init__(
        self,
        script: list[dict[str, Any]],
        statuses: list[dict[str, Any]],
        *,
        capture_error: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(script, **kwargs)
        self.statuses = statuses
        self.status_calls = 0
        self.capture_error = capture_error

    def _status(self) -> dict[str, Any]:
        index = min(self.status_calls, len(self.statuses) - 1)
        self.status_calls += 1
        return self.statuses[index]

    def _capture(self, game_id: str):
        if self.capture_error:
            self.calls += 1
            return None, None, None, self.capture_error
        return super()._capture(game_id)


def _wait(session: ScriptedSession, timeout: float):
    return session._wait_for_game(
        game_id="test-game",
        title="Test Game",
        started=time.monotonic(),
        timeout=timeout,
    )


def test_boot_screen_only_is_not_success(tmp_path: Path) -> None:
    session = ScriptedSession(
        [{"digest": BOOT_DIGEST, "pc": TOS_PC}],
        state_dir=tmp_path,
        boot_timeout=2.0,
    )
    result = _wait(session, 1.4)

    assert not result.ok
    assert result.failure_reason == "timeout_no_stable_frame"
    assert result.evidence["classification"] == "boot_screen_only"
    assert result.evidence["seen_ram_pc"] is False


def test_changed_frame_with_ram_pc_succeeds(tmp_path: Path) -> None:
    session = ScriptedSession(
        [
            {"digest": BOOT_DIGEST, "pc": TOS_PC},
            {"digest": GAME_DIGEST, "pc": RAM_PC},
        ],
        state_dir=tmp_path,
        boot_timeout=4.0,
    )
    result = _wait(session, 3.0)

    assert result.ok, result.message
    assert result.evidence["pc_in_ram"] is True
    assert result.evidence["frame_changed"] is True
    assert "game frame" in result.message


def test_changed_frame_while_pc_stays_in_tos_is_not_success(tmp_path: Path) -> None:
    session = ScriptedSession(
        [
            {"digest": BOOT_DIGEST, "pc": TOS_PC},
            {"digest": GAME_DIGEST, "pc": TOS_PC},
        ],
        state_dir=tmp_path,
        boot_timeout=2.0,
    )
    result = _wait(session, 1.6)

    assert not result.ok
    assert result.failure_reason == "timeout_no_stable_frame"
    assert result.evidence["classification"] == "no_game_pc"
    assert result.evidence["seen_ram_pc"] is False


def test_stable_loader_after_ram_pc_succeeds(tmp_path: Path) -> None:
    session = ScriptedSession(
        [
            {"digest": BOOT_DIGEST, "pc": TOS_PC},
            {"digest": GAME_DIGEST, "pc": RAM_PC},
            {"digest": GAME_DIGEST, "pc": TOS_PC},
        ],
        state_dir=tmp_path,
        boot_timeout=5.0,
    )
    result = _wait(session, 4.2)

    assert result.ok, result.message
    assert result.evidence["mode"] == "loader"
    assert result.evidence["seen_ram_pc"] is True
    assert "loader frame" in result.message


def test_missing_pc_uses_conservative_frame_fallback(tmp_path: Path) -> None:
    session = ScriptedSession(
        [
            {"digest": BOOT_DIGEST, "pc": None},
            {"digest": GAME_DIGEST, "pc": None},
        ],
        state_dir=tmp_path,
        boot_timeout=5.0,
    )
    result = _wait(session, 4.2)

    assert result.ok, result.message
    assert result.evidence["pc"] is None
    assert result.evidence["pc_in_ram"] is False


def test_status_bar_only_with_blank_content_is_not_success(tmp_path: Path) -> None:
    session = ScriptedSession(
        [{"digest": BOOT_DIGEST, "pc": RAM_PC, "content_ratio": 0.0, "colors": 1}],
        state_dir=tmp_path,
        boot_timeout=1.0,
    )
    result = _wait(session, 0.8)

    assert not result.ok
    assert result.failure_reason == "timeout_blank_screen"


def test_terminal_state_still_fails(tmp_path: Path) -> None:
    session = ScriptedSession(
        [{"digest": BOOT_DIGEST, "pc": TOS_PC}],
        state_dir=tmp_path,
    )
    session.status = {"state": "exited", "exit_reason": "requested_stop", "returncode": 0}

    result = _wait(session, 1.0)

    assert not result.ok
    assert result.failure_reason == "requested_stop"
    assert "exited" in result.message


def test_process_exit_after_control_timeout_is_guest_cpu_halt(tmp_path: Path) -> None:
    session = SequencedStatusSession(
        [{"digest": BOOT_DIGEST, "pc": TOS_PC}],
        [
            {"state": "running"},
            {
                "state": "exited",
                "exit_reason": "process_exit",
                "exit_cause": "process_exit",
                "returncode": 0,
            },
        ],
        capture_error="timeout: Debugger command timed out after 15.0s.",
        state_dir=tmp_path,
    )

    result = _wait(session, 1.0)

    assert not result.ok
    assert result.failure_reason == "guest_cpu_halt"
    assert "double bus/address error" in result.message
    assert result.evidence["halt_dialog_suspected"] is True
    assert result.evidence["capture_failures"] == 1


def test_clean_process_exit_without_timeout_stays_process_exit(tmp_path: Path) -> None:
    session = SequencedStatusSession(
        [{"digest": BOOT_DIGEST, "pc": TOS_PC}],
        [
            {"state": "running"},
            {
                "state": "exited",
                "exit_reason": "process_exit",
                "exit_cause": "process_exit",
                "returncode": 0,
            },
        ],
        state_dir=tmp_path,
    )

    result = _wait(session, 1.0)

    assert not result.ok
    assert result.failure_reason == "process_exit"
    assert result.evidence["halt_dialog_suspected"] is False


def test_requested_stop_is_never_reported_as_guest_cpu_halt(tmp_path: Path) -> None:
    session = SequencedStatusSession(
        [{"digest": BOOT_DIGEST, "pc": TOS_PC}],
        [
            {"state": "running"},
            {
                "state": "exited",
                "exit_reason": "requested_stop",
                "exit_cause": "requested_stop",
                "returncode": 0,
            },
        ],
        capture_error="timeout: Debugger command timed out after 15.0s.",
        state_dir=tmp_path,
    )

    result = _wait(session, 1.0)

    assert not result.ok
    assert result.failure_reason == "requested_stop"
    assert result.evidence["halt_dialog_suspected"] is False
