from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from hatari_mcp.services.scenario import CaseRun, ScenarioEngine, ScenarioError, expand_matrix, load_scenario, resolve_case
from hatari_mcp.tools import frames


def make_bmp_bytes() -> bytes:
    target = Path("/tmp") / "scenario-test-frame.bmp"
    frames.write_bmp24(
        target,
        2,
        1,
        bytes([10, 20, 30, 255, 40, 50, 60, 255]),
    )
    data = target.read_bytes()
    target.unlink(missing_ok=True)
    return data


class FakeBackend:
    def __init__(self, *, fail_stop: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.state = "stopped"
        self.connected = False
        self.closed = False
        self.fail_stop = fail_stop
        self.bmp = make_bmp_bytes()
        self.shot_dir: Path | None = None
        self.shot_count = 0

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.closed = True

    async def call_tool(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        args = dict(args or {})
        self.calls.append((name, args))
        if name == "emu_wait_for_exit" and args.get("hang") is True:
            await asyncio.sleep(10)
        if name == "emu_start":
            self.state = "running"
            args_list = list(args.get("args") or [])
            for index, token in enumerate(args_list):
                if token == "--screenshot-dir" and index + 1 < len(args_list):
                    self.shot_dir = Path(args_list[index + 1])
            return {"ok": True, "state": "running", "pid": 4242, "socket": "/tmp/fake/control.sock"}
        if name == "emu_shortcut" and args.get("name") == "screenshot" and self.shot_dir:
            self.shot_dir.mkdir(parents=True, exist_ok=True)
            self.shot_count += 1
            (self.shot_dir / f"grab{self.shot_count:04d}.bmp").write_bytes(self.bmp)
            return {"ok": True, "shortcut": "screenshot"}
        if name == "emu_status":
            return {"ok": True, "state": self.state}
        if name == "emu_pause":
            self.state = "remote_paused"
            return {"ok": True, "state": self.state}
        if name == "emu_resume":
            self.state = "running"
            return {"ok": True, "state": self.state}
        if name == "emu_stop":
            if self.fail_stop:
                raise ScenarioError("tool_error", "stop failed")
            self.state = "exited"
            return {"ok": True, "state": "exited"}
        if name == "dbg_command":
            command = args.get("command", "")
            if command.startswith("screenshot "):
                target = Path(command[len("screenshot ") :])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(self.bmp)
            return {"ok": True, "raw": ""}
        if name == "log_read":
            return {"ok": True, "entries": [{"kind": "log", "text": "hello"}]}
        if name == "console_read":
            return {"ok": True, "entries": [{"kind": "console", "text": "READY"}]}
        return {"ok": True, "tool": name, "args": args, "state": self.state}


def scenario_data(**overrides):
    data = {
        "version": 1,
        "id": "runner-test",
        "name": "Runner test",
        "timeouts": {"scenario_ms": 5000, "step_ms": 2000},
        "steps": [
            {"id": "start", "tool": "emu_start", "args": {}},
            {"id": "capture", "tool": "screen_frame_capture", "args": {}, "timeout_ms": 2000},
            {"id": "stop", "tool": "emu_stop", "args": {}},
        ],
        "teardown": [{"id": "teardown", "tool": "emu_stop", "args": {"force": True}}],
    }
    data.update(overrides)
    return data


def run_scenario(tmp_path, data, backend=None, jobs=1):
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    created: list[FakeBackend] = []

    def factory():
        item = backend or FakeBackend()
        if backend is None:
            created.append(item)
        return item

    engine = ScenarioEngine(backend_factory=factory, jobs=jobs, run_id="unit-run")
    result = asyncio.run(engine.run(path, artifacts_root=tmp_path / "artifacts"))
    return result, created


def test_successful_run_captures_artifact_and_teardown(tmp_path):
    result, created = run_scenario(tmp_path, scenario_data())
    assert result.exit_code == 0
    assert result.cases[0]["status"] == "passed"
    bundle = Path(result.artifacts) / "runner-test" / "attempt-1"
    assert (bundle / ".complete").is_file()
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["status"] == "passed"
    assert any(item["path"] == "screenshots/capture.bmp" for item in manifest["artifacts"])
    assert sum(1 for name, _ in created[0].calls if name == "emu_stop") >= 1
    assert created[0].closed is True


def test_step_failure_reports_location_and_keeps_bundle(tmp_path):
    data = scenario_data()
    data["steps"].insert(1, {"id": "bad", "tool": "emu_status", "args": {}, "expect": {"state": "nope"}})
    result, _ = run_scenario(tmp_path, data)

    assert result.exit_code == 1
    case = result.cases[0]
    assert case["status"] == "failed"
    assert case["failure"]["step_id"] == "bad"
    assert case["failure"]["step_index"] == 1
    assert case["failure"]["phase"] == "emu_status"
    bundle = Path(tmp_path) / "artifacts" / "unit-run" / "runner-test" / "attempt-1"
    assert (bundle / ".complete").is_file()
    assert json.loads((bundle / "manifest.json").read_text())["status"] == "failed"


def test_step_timeout_still_runs_teardown(tmp_path):
    data = scenario_data()
    data["steps"] = [
        {"id": "hang", "tool": "emu_wait_for_exit", "args": {"hang": True}, "timeout_ms": 50},
    ]
    result, created = run_scenario(tmp_path, data)

    assert result.cases[0]["status"] == "failed"
    assert result.cases[0]["failure"]["error"] == "step_timeout"
    names = [name for name, _ in created[0].calls]
    assert "emu_stop" in names
    assert created[0].closed is True


def test_scenario_timeout_is_enforced(tmp_path):
    data = scenario_data(timeouts={"scenario_ms": 80, "step_ms": 5000})
    data["steps"] = [{"id": "hang", "tool": "emu_wait_for_exit", "args": {"hang": True}, "timeout_ms": 5000}]
    result, _ = run_scenario(tmp_path, data)
    assert result.cases[0]["status"] == "failed"
    assert result.cases[0]["failure"]["error"] == "step_timeout"
    assert result.cases[0]["failure"]["step_id"] == "hang"


def test_teardown_error_is_secondary_and_does_not_mask_primary(tmp_path):
    backend = FakeBackend(fail_stop=True)
    data = scenario_data()
    data["steps"].insert(1, {"id": "bad", "tool": "emu_status", "args": {}, "expect": {"state": "nope"}})
    result, _ = run_scenario(tmp_path, data, backend=backend)
    failure = result.cases[0]["failure"]
    assert failure["error"] == "assertion_failed"
    assert failure["secondary_errors"]
    assert failure["secondary_errors"][0]["step_id"] == "teardown"


def test_skip_when_required_env_missing(tmp_path):
    data = scenario_data(
        requires={
            "env": ["A_MISSING_ENV_FOR_TEST"],
            "skip_reason": "test prerequisite missing",
        }
    )
    result, created = run_scenario(tmp_path, data)
    assert result.cases[0]["status"] == "skipped"
    assert result.cases[0]["failure"]["message"] == "test prerequisite missing"
    assert created == []
    bundle = Path(tmp_path) / "artifacts" / "unit-run" / "runner-test" / "attempt-1"
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["status"] == "skipped"
    assert manifest["skip_reason"] == "test prerequisite missing"


def test_matrix_parallel_cases_get_isolated_backends(tmp_path):
    data = scenario_data(matrix={"machine": ["st", "ste"]})
    data["steps"] = [{"id": "start", "tool": "emu_start", "args": {}}]
    result, created = run_scenario(tmp_path, data, jobs=2)
    assert result.exit_code == 0
    assert len(result.cases) == 2
    assert len(created) == 2
    assert sorted(case["case"] for case in result.cases) == [
        "runner-test_machine_st",
        "runner-test_machine_ste",
    ]


def test_cancelled_case_runs_teardown(tmp_path):
    data = scenario_data()
    data["steps"] = [{"id": "hang", "tool": "emu_wait_for_exit", "args": {"hang": True}, "timeout_ms": 5000}]
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    loaded = load_scenario(path)
    resolved = resolve_case(expand_matrix(loaded)[0], check_paths=False)
    backend = FakeBackend()

    async def main():
        runner = CaseRun(
            resolved,
            run_id="cancel-run",
            artifacts_root=tmp_path / "artifacts",
            backend_factory=lambda: backend,
        )
        task = asyncio.create_task(runner.run())
        await asyncio.sleep(0.1)
        task.cancel()
        return await task

    result = asyncio.run(main())
    assert result["status"] == "cancelled"
    assert backend.closed is True
    assert ("emu_stop", {"force": True}) in backend.calls


def test_staged_workspace_is_cleaned_up(tmp_path):
    source = tmp_path / "corpus-file.bin"
    source.write_bytes(b"corpus")
    data = scenario_data()
    data["steps"] = [{"id": "start", "tool": "emu_start", "args": {}}]
    data["stage"] = [{"from": str(source), "to": "staged/file.bin"}]
    result, _ = run_scenario(tmp_path, data)
    assert result.cases[0]["status"] == "passed"
    bundle = Path(tmp_path) / "artifacts" / "unit-run" / "runner-test" / "attempt-1"
    stage = json.loads((bundle / "stage.json").read_text())
    assert Path(stage["workspace"]).exists() is False
    assert (bundle / ".complete").is_file()
