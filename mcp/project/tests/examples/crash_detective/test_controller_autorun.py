"""Autorun path plumbing for crash-detective disk launches."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from crash_detective import controller
from crash_detective.controller import CrashDetective
from crash_detective.discovery import make_entry


class FakeSession:
    instances: list["FakeSession"] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs
        self.current_disk = None
        self.start_kwargs: dict[str, Any] = {}
        FakeSession.instances.append(self)

    def start_game(self, entry: Any, **kwargs: Any) -> Any:
        self.start_kwargs = kwargs
        return SimpleNamespace(ok=True, evidence={}, status="success", message="")

    def stop(self, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True}


def _fake_detective(tmp_path: Path) -> CrashDetective:
    detective = CrashDetective(state_dir=tmp_path, client=object())
    detective._safe_status = lambda client: {"state": "running"}
    detective._read_logs = lambda client: {"stderr": [], "console": []}
    detective._gather_frames = lambda result, session, frames_dir: []
    detective._disk_game = lambda entry: {"id": "game", "title": "Game"}
    detective._disk_source = lambda entry, session: {"kind": "disk"}
    return detective


def test_launch_disk_passes_normalised_autorun_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(controller, "GameSession", FakeSession)
    entry = make_entry(tmp_path / "Game (1990)(Pub).st", tmp_path)
    detective = _fake_detective(tmp_path)
    captured: dict[str, Any] = {}

    def fake_record(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"run_id": "run-1", "result": {}, "launch": kwargs["launch"]}

    detective._record = fake_record  # type: ignore[method-assign]
    detective._finalize = lambda record, **kwargs: {"record": record}  # type: ignore[method-assign]

    payload = detective.launch_disk(entry, autorun_path="runme.prg")

    assert FakeSession.instances[-1].start_kwargs["autostart"] == "A:\\runme.prg"
    assert captured["launch"]["autorun_path"] == "A:\\runme.prg"
    assert payload["record"]["launch"]["autorun_path"] == "A:\\runme.prg"


def test_invalid_autorun_path_fails_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(controller, "GameSession", FakeSession)
    entry = make_entry(tmp_path / "Game (1990)(Pub).st", tmp_path)
    detective = _fake_detective(tmp_path)
    FakeSession.instances.clear()

    with pytest.raises(ValueError, match="outside|inside|control"):
        detective.launch_disk(entry, autorun_path="../escape.prg")

    assert not FakeSession.instances
