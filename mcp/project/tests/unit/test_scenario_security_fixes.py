from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import hatari_mcp.services.scenario as scenario_module
from hatari_mcp.config import Settings
from hatari_mcp.corpus import stage_files
from hatari_mcp.services.scenario import (
    ArtifactBundle,
    ArtifactQuotaExceeded,
    CaseRun,
    RunBudget,
    ScenarioEngine,
    ScenarioError,
    _copy_file_checked,
    _verify_bundle_checksums,
    default_server_env,
    expand_matrix,
    load_scenario,
    parse_scenario,
    replay_bundle,
    resolve_case,
)


def scenario_data(**overrides: Any) -> dict[str, Any]:
    data = {
        "version": 1,
        "id": "security",
        "name": "Security",
        "timeouts": {"scenario_ms": 2000, "step_ms": 1000},
        "steps": [
            {"id": "status", "tool": "emu_status", "args": {}},
        ],
    }
    data.update(overrides)
    return data


def write(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class RecordingBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False
        self.state = "stopped"

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True

    async def call_tool(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((name, dict(args or {})))
        if name == "emu_start":
            self.state = "running"
            return {"ok": True, "state": "running"}
        if name == "emu_status":
            return {"ok": True, "state": self.state}
        if name == "emu_stop":
            self.state = "exited"
            return {"ok": True, "state": "exited"}
        return {"ok": True}


def run_engine(tmp_path: Path, data: dict[str, Any], backend: Any | None = None) -> tuple[Any, RecordingBackend]:
    path = write(tmp_path, data)
    item = backend or RecordingBackend()
    engine = ScenarioEngine(backend_factory=lambda: item, run_id="security-run", jobs=1)
    result = asyncio.run(engine.run(path, artifacts_root=tmp_path / "artifacts"))
    return result, item


def test_corpus_workspace_is_rejected_even_with_declared_env(tmp_path: Path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    data = scenario_data(
        requires={"env": ["OKAMI_ROOT"]},
        config={"workspace": "${OKAMI_ROOT}"},
    )
    loaded = parse_scenario(data, source=(tmp_path / "scenario.json"))
    with pytest.raises(ScenarioError) as excinfo:
        resolve_case(
            expand_matrix(loaded)[0],
            environment={"OKAMI_ROOT": str(corpus)},
            check_paths=True,
        )
    assert excinfo.value.code == "corpus_write_risk"


def test_artifact_root_inside_repo_is_rejected(tmp_path: Path):
    repo = Settings.load().repo_root
    with pytest.raises(ScenarioError) as excinfo:
        ArtifactBundle.create(repo / "p6-forbidden-artifacts", "run", "case")
    assert excinfo.value.code == "corpus_write_risk"
    assert not (repo / "p6-forbidden-artifacts").exists()


def test_step_level_emu_start_raw_args_are_validated(tmp_path: Path):
    data = scenario_data(
        steps=[
            {
                "id": "start",
                "tool": "emu_start",
                "args": {"args": ["--harddrive", str(tmp_path)]},
            }
        ]
    )
    loaded = parse_scenario(data, source=tmp_path / "scenario.json")
    with pytest.raises(ScenarioError) as excinfo:
        resolve_case(expand_matrix(loaded)[0], check_paths=True)
    assert excinfo.value.code == "unsafe_argument"


def test_emu_start_unknown_keys_are_rejected():
    data = scenario_data(
        steps=[{"id": "start", "tool": "emu_start", "args": {"mystery": True}}]
    )
    with pytest.raises(ScenarioError) as excinfo:
        parse_scenario(data)
    assert excinfo.value.code == "schema_error"


def test_config_and_step_args_are_merged_and_validated(tmp_path: Path):
    data = scenario_data(
        config={"args": ["--sound", "off"]},
        steps=[
            {"id": "start", "tool": "emu_start", "args": {"args": ["--monitor", "rgb"]}},
            {"id": "stop", "tool": "emu_stop", "args": {}},
        ],
    )
    result, backend = run_engine(tmp_path, data)
    assert result.exit_code == 0
    start = next(args for name, args in backend.calls if name == "emu_start")
    assert "--sound" in start["args"]
    assert "--monitor" in start["args"]


@pytest.mark.parametrize("tool", ["dbg_command", "ahcc_compile", "scenario_run", "emu_set_option", "emu_set_path", "dbg_memory_write"])
def test_privileged_step_tools_are_denied(tool: str):
    data = scenario_data(steps=[{"id": "bad", "tool": tool, "args": {}}])
    with pytest.raises(ScenarioError) as excinfo:
        parse_scenario(data)
    assert excinfo.value.code == "tool_not_allowed"


def test_recursion_limit_is_enforced(tmp_path: Path):
    path = write(tmp_path, scenario_data())
    engine = ScenarioEngine(backend_factory=lambda: RecordingBackend(), max_depth=0)
    with pytest.raises(ScenarioError) as excinfo:
        asyncio.run(engine.run(path, artifacts_root=tmp_path / "artifacts"))
    assert excinfo.value.code == "recursion_limit"


def test_artifact_case_dir_symlink_is_rejected(tmp_path: Path):
    root = tmp_path / "artifacts"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "case_one").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ScenarioError) as excinfo:
        ArtifactBundle.create(root, "run", "case/one")
    assert excinfo.value.code == "path_not_allowed"


def _replay_bundle(
    tmp_path: Path,
    *,
    events: list[dict[str, Any]] | None = None,
    checkpoints: list[dict[str, Any]] | None = None,
    start: dict[str, Any] | None = None,
) -> Path:
    bundle = ArtifactBundle.create(tmp_path / "artifacts", "run", "case")
    bundle.write_json(
        "replay/manifest.json",
        {"schema": "hatari-mcp.replay/1", "version": 1, "scenario": "demo", "start": start or {"profile": "st"}},
    )
    bundle.write_text(
        "replay/events.jsonl",
        "".join(json.dumps(item) + "\n" for item in (events or [])),
    )
    bundle.write_text(
        "replay/checkpoints.jsonl",
        "".join(json.dumps(item) + "\n" for item in (checkpoints or [])),
    )
    return bundle.finalize("passed", {"scenario": "demo"})


def test_replay_requires_complete_bundle_and_checkpoints(tmp_path: Path):
    bundle = _replay_bundle(tmp_path)
    with pytest.raises(ScenarioError) as excinfo:
        asyncio.run(replay_bundle(bundle, backend_factory=RecordingBackend))
    assert excinfo.value.code == "replay_unverified"

    bundle2 = _replay_bundle(tmp_path / "two")
    (bundle2 / ".complete").unlink()
    with pytest.raises(ScenarioError) as excinfo:
        asyncio.run(replay_bundle(bundle2, backend_factory=RecordingBackend))
    assert excinfo.value.code == "replay_incomplete"


def test_replay_rejects_disallowed_event_tool(tmp_path: Path):
    bundle = _replay_bundle(
        tmp_path,
        events=[{"tool": "dbg_command", "args": {"command": "quit"}}],
        checkpoints=[{"kind": "screen_rgba_sha256", "value": "0" * 64, "step_id": "cap"}],
    )
    with pytest.raises(ScenarioError) as excinfo:
        asyncio.run(replay_bundle(bundle, backend_factory=RecordingBackend))
    assert excinfo.value.code == "tool_not_allowed"


def test_replay_rejects_tampered_bundle(tmp_path: Path):
    bundle = _replay_bundle(
        tmp_path,
        checkpoints=[{"kind": "screen_rgba_sha256", "value": "0" * 64, "step_id": "cap"}],
    )
    (bundle / "replay" / "events.jsonl").write_text('{"tool":"input_text","args":{"text":"evil"}}\n', encoding="utf-8")
    with pytest.raises(ScenarioError) as excinfo:
        asyncio.run(replay_bundle(bundle, backend_factory=RecordingBackend))
    assert excinfo.value.code == "replay_untrusted"


def test_matrix_cardinality_is_computed_before_expansion():
    data = scenario_data(matrix={"a": list(range(300)), "b": list(range(300))})
    loaded = parse_scenario(data)
    with pytest.raises(ScenarioError) as excinfo:
        expand_matrix(loaded)
    assert excinfo.value.code == "matrix_too_large"
    assert "90000" in excinfo.value.message


def test_matrix_meta_cap_is_enforced():
    data = scenario_data(matrix={"a": [1, 2, 3], "max_cases": 2})
    loaded = parse_scenario(data)
    with pytest.raises(ScenarioError) as excinfo:
        expand_matrix(loaded)
    assert excinfo.value.code == "matrix_too_large"


def test_duplicate_matrix_values_and_sanitized_ids_are_rejected():
    duplicate = scenario_data(matrix={"a": [1, 1]})
    with pytest.raises(ScenarioError) as excinfo:
        parse_scenario(duplicate)
    assert excinfo.value.code == "duplicate_matrix_value"

    collision = scenario_data(matrix={"a": ["a b", "a_b"]})
    loaded = parse_scenario(collision)
    with pytest.raises(ScenarioError) as excinfo:
        expand_matrix(loaded)
    assert excinfo.value.code == "duplicate_case_id"


def test_run_budget_is_enforced(tmp_path: Path):
    budget = RunBudget(5)
    bundle = ArtifactBundle.create(tmp_path / "artifacts", "run", "case", run_budget=budget)
    with pytest.raises(ArtifactQuotaExceeded):
        bundle.write_bytes("big.bin", b"x" * 10)
    assert budget.exceeded is True


def test_server_child_env_is_allowlisted():
    env = default_server_env({"PATH": "/bin", "MY_SECRET_TOKEN": "do-not-leak", "HATARI_TOS": "/tmp/tos.img"})
    assert env["PATH"] == "/bin"
    assert env["HATARI_TOS"] == "/tmp/tos.img"
    assert "MY_SECRET_TOKEN" not in env


def test_resolved_snapshot_redacts_expanded_secrets(tmp_path: Path):
    data = scenario_data(
        requires={"env": ["API_TOKEN"]},
        steps=[{"id": "type", "tool": "input_text", "args": {"text": "${API_TOKEN}"}}],
    )
    loaded = parse_scenario(data, source=tmp_path / "scenario.json")
    resolved = resolve_case(expand_matrix(loaded)[0], environment={"API_TOKEN": "super-secret-value"}, check_paths=True)
    assert resolved.resolved["steps"][0]["args"]["text"] == "<redacted>"
    assert "super-secret-value" not in json.dumps(resolved.resolved)


def test_missing_required_rom_is_an_error_not_a_skip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(CaseRun, "_find_tos", lambda self: None)
    data = scenario_data(requires={"rom": "emutos"})
    result, _ = run_engine(tmp_path, data)
    assert result.cases[0]["status"] == "error"
    assert result.cases[0]["failure"]["error"] == "missing_rom"


def test_connect_timeout_is_bounded(tmp_path: Path):
    class SlowBackend(RecordingBackend):
        async def connect(self) -> None:
            await asyncio.sleep(10)

    data = scenario_data(timeouts={"scenario_ms": 80, "step_ms": 80})
    result, _ = run_engine(tmp_path, data, backend=SlowBackend())
    assert result.cases[0]["status"] == "error"
    assert result.cases[0]["failure"]["error"] == "connect_timeout"


def test_invalid_regex_and_png_rgba_are_schema_errors():
    regex = scenario_data(
        steps=[{"id": "s", "tool": "emu_status", "args": {}, "assert": {"path": "state", "regex": "("}}]
    )
    with pytest.raises(ScenarioError) as excinfo:
        parse_scenario(regex)
    assert excinfo.value.code == "schema_error"

    png = scenario_data(
        steps=[
            {
                "id": "cap",
                "tool": "screen_frame_capture",
                "args": {},
                "capture": {"name": "cap", "kind": "screenshot", "format": "png", "hash": "rgba"},
            }
        ]
    )
    with pytest.raises(ScenarioError) as excinfo:
        parse_scenario(png)
    assert excinfo.value.code == "schema_error"


def test_copy_file_confines_sources(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    bundle = ArtifactBundle.create(tmp_path / "artifacts", "run", "case", allowed_sources=[workspace])
    with pytest.raises(ScenarioError) as excinfo:
        bundle.copy_file("artifacts/outside.bin", outside)
    assert excinfo.value.code == "path_not_allowed"


def test_corpus_stage_rejects_symlinks_and_corpus_workspace(tmp_path: Path):
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "file.bin").write_bytes(b"data")
    (root / "link.bin").symlink_to(root / "file.bin")
    with pytest.raises(ValueError):
        stage_files(root, ["link.bin"], tmp_path / "workspace")
    with pytest.raises(ValueError):
        stage_files(root, ["file.bin"], root / "workspace")


def test_artifact_root_symlink_is_rejected(tmp_path: Path):
    real = tmp_path / "real-root"
    real.mkdir()
    link = tmp_path / "link-root"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(ScenarioError) as excinfo:
        ArtifactBundle.create(link, "run", "case")
    assert excinfo.value.code == "path_not_allowed"


def test_media_path_outside_roots_is_rejected(tmp_path: Path):
    data = scenario_data(
        steps=[{"id": "flop", "tool": "media_insert_floppy", "args": {"path": "/etc/hostname"}}]
    )
    loaded = parse_scenario(data, source=tmp_path / "scenario.json")
    with pytest.raises(ScenarioError) as excinfo:
        resolve_case(expand_matrix(loaded)[0], check_paths=True)
    assert excinfo.value.code == "path_not_allowed"


def test_staging_rejects_symlinked_source_tree(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "real.bin").write_bytes(b"data")
    (source / "leak.bin").symlink_to("/etc/hostname")
    data = scenario_data(
        steps=[{"id": "status", "tool": "emu_status", "args": {}}],
        stage=[{"from": str(source), "to": "staged"}],
    )
    result, _ = run_engine(tmp_path, data)
    assert result.cases[0]["status"] == "failed"
    assert result.cases[0]["failure"]["error"] == "path_not_allowed"


def test_finalize_failure_is_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def boom(self, status, manifest):
        raise OSError("disk full")

    monkeypatch.setattr(ArtifactBundle, "finalize", boom)
    result, _ = run_engine(tmp_path, scenario_data())
    assert result.cases[0]["status"] == "error"
    assert any(item["error"] == "finalize_failed" for item in result.cases[0]["secondary_errors"])


UNSAFE_HATARI_ARGS = [
    ["--parse", "/tmp/commands.dbg"],
    ["--parse", "prg:/tmp/commands.dbg"],
    ["--debug"],
    ["--debug", "off"],
    ["-D"],
    ["-d", "/tmp"],
    ["-h"],
    ["-v"],
    ["-c", "/tmp/config"],
    ["-k", "/tmp/keymap"],
    ["-m"],
    ["-f"],
    ["-w"],
    ["-z", "2"],
    ["-j", "0"],
    ["-s", "1024"],
    ["-t", "/tmp/tos.img"],
    ["--bios-intercept", "on"],
    ["--natfeats", "on"],
    ["--conout", "2"],
    ["--disasm", "uae"],
    ["--memconv", "on"],
    ["--saveconfig"],
    ["--control-socket", "/tmp/hijack.sock"],
    ["--cmd-fifo", "/tmp/hijack.fifo"],
    ["--configfile", "/tmp/host.cfg"],
    ["--keymap", "/tmp/keymap"],
    ["--cartridge", "/tmp/cart.img"],
    ["--harddrive", "/tmp"],
    ["--acsi", "0=/etc/passwd"],
    ["--scsi", "0=/etc/passwd"],
    ["--ide-master", "/tmp/disk.img"],
    ["--ide-slave", "/tmp/disk.img"],
    ["--disk-a", "/tmp/disk.st"],
    ["--disk-b", "/tmp/disk.st"],
    ["--printer", "/tmp/print.out"],
    ["--midi-in", "/tmp/midi.in"],
    ["--midi-out", "/tmp/midi.out"],
    ["--rs232-in", "/tmp/serial.in"],
    ["--rs232-out", "/tmp/serial.out"],
    ["--scc-a-in", "/tmp/scc-a.in"],
    ["--scc-a-out", "/tmp/scc-a.out"],
    ["--scc-b-out", "/tmp/scc-b.out"],
    ["--scc-a-lan-out", "/tmp/scc-lan.out"],
    ["--avi-file", "/tmp/out.avi"],
    ["--avirecord", "on"],
    ["--avi-vcodec", "png"],
    ["--png-level", "9"],
    ["--avi-fps", "25"],
    ["--screenshot-dir", "/tmp/out"],
    ["--screenshot-format", "png"],
    ["--memstate", "/tmp/state.sav"],
    ["--trace", "cpu_all"],
    ["--trace-file", "/tmp/trace.txt"],
    ["--log-file", "/tmp/host.log"],
    ["--alert-level", "debug"],
    ["--debug-except", "all"],
    ["--symload", "exec"],
    ["--lilo", "/tmp/linux"],
    ["--auto", "C:\\EVIL.PRG"],
    ["--parse=/tmp/commands.dbg"],
    ["--acsi=0=/etc/passwd"],
]


@pytest.mark.parametrize("args", UNSAFE_HATARI_ARGS)
def test_dangerous_hatari_options_are_denied(args: list[str]):
    data = scenario_data(config={"args": args})
    with pytest.raises(ScenarioError) as excinfo:
        parse_scenario(data)
    assert excinfo.value.code == "unsafe_argument"


def test_step_level_and_determinism_args_use_the_same_allowlist():
    step_data = scenario_data(
        steps=[{"id": "start", "tool": "emu_start", "args": {"args": ["--parse", "/tmp/x.dbg"]}}]
    )
    loaded = parse_scenario(step_data)
    with pytest.raises(ScenarioError) as excinfo:
        resolve_case(expand_matrix(loaded)[0], check_paths=True)
    assert excinfo.value.code == "unsafe_argument"

    det_data = scenario_data(determinism={"mode": "strict", "args": ["--debug"]})
    with pytest.raises(ScenarioError) as excinfo:
        parse_scenario(det_data)
    assert excinfo.value.code == "unsafe_argument"


def test_replay_start_args_are_validated(tmp_path: Path):
    bundle = _replay_bundle(
        tmp_path,
        checkpoints=[{"kind": "screen_rgba_sha256", "value": "0" * 64, "step_id": "cap"}],
        start={"profile": "st", "args": ["--parse", "/tmp/commands.dbg"]},
    )
    with pytest.raises(ScenarioError) as excinfo:
        asyncio.run(replay_bundle(bundle, backend_factory=RecordingBackend))
    assert excinfo.value.code == "unsafe_argument"


def test_recording_and_save_shortcuts_are_denied():
    for name in ("savemem", "recanim", "recsound", "Savemem"):
        data = scenario_data(steps=[{"id": "shortcut", "tool": "emu_shortcut", "args": {"name": name}}])
        loaded = parse_scenario(data)
        with pytest.raises(ScenarioError) as excinfo:
            resolve_case(expand_matrix(loaded)[0], check_paths=True)
        assert excinfo.value.code == "unsafe_argument"


def test_workspaces_must_be_fresh_temp_directories(tmp_path: Path):
    data = scenario_data(config={"workspace": str(tmp_path)})
    (tmp_path / "host-file").write_text("not a workspace", encoding="utf-8")
    loaded = parse_scenario(data, source=tmp_path / "scenario.json")
    with pytest.raises(ScenarioError) as excinfo:
        resolve_case(expand_matrix(loaded)[0], check_paths=True)
    assert excinfo.value.code == "path_not_allowed"

    step_data = scenario_data(
        steps=[{"id": "start", "tool": "emu_start", "args": {"workspace": str(tmp_path)}}]
    )
    loaded = parse_scenario(step_data, source=tmp_path / "scenario.json")
    with pytest.raises(ScenarioError) as excinfo:
        resolve_case(expand_matrix(loaded)[0], check_paths=True)
    assert excinfo.value.code == "path_not_allowed"


def test_fresh_empty_temp_workspace_is_accepted(tmp_path: Path):
    workspace = tmp_path / "fresh"
    workspace.mkdir()
    data = scenario_data(config={"workspace": str(workspace)})
    loaded = parse_scenario(data, source=tmp_path / "scenario.json")
    resolved = resolve_case(expand_matrix(loaded)[0], check_paths=True)
    assert resolved.config["workspace"] == str(workspace.resolve())


def test_declared_env_values_are_redacted_from_every_text_artifact(tmp_path: Path):
    credential = tmp_path / "credential.bin"
    credential.write_bytes(b"payload")
    secret_value = str(credential)
    data = scenario_data(
        requires={"env": ["AWS_ACCESS_KEY_ID"]},
        record=True,
        stage=[{"from": "${AWS_ACCESS_KEY_ID}", "to": "payload.bin"}],
        steps=[
            {"id": "start", "tool": "emu_start", "args": {}},
            {"id": "type", "tool": "input_text", "args": {"text": "${AWS_ACCESS_KEY_ID}"}},
            {"id": "stop", "tool": "emu_stop", "args": {}},
        ],
    )
    path = write(tmp_path, data)
    backend = RecordingBackend()
    engine = ScenarioEngine(
        backend_factory=lambda: backend,
        run_id="redact-run",
        environment={"AWS_ACCESS_KEY_ID": secret_value},
    )
    result = asyncio.run(engine.run(path, artifacts_root=tmp_path / "artifacts"))
    assert result.exit_code == 0
    bundle = Path(result.artifacts) / result.cases[0]["artifacts"]
    for artifact in bundle.rglob("*"):
        if artifact.is_file() and artifact.suffix in {".json", ".jsonl", ".log", ".txt", ".xml"}:
            assert secret_value not in artifact.read_text(encoding="utf-8"), artifact
    for rel in ("replay/events.jsonl", "logs/events.jsonl", "stage.json", "scenario.resolved.json"):
        assert (bundle / rel).is_file(), rel
    assert "<redacted>" in (bundle / "stage.json").read_text(encoding="utf-8")
    assert "<redacted>" in (bundle / "replay" / "events.jsonl").read_text(encoding="utf-8")


def test_checksum_verification_rejects_escape_symlink_and_missing_coverage(tmp_path: Path):
    bundle = _replay_bundle(
        tmp_path,
        checkpoints=[{"kind": "screen_rgba_sha256", "value": "0" * 64, "step_id": "cap"}],
    )
    checksum = bundle / "checksums.sha256"
    original = checksum.read_text(encoding="utf-8")

    absolute = f"{hashlib.sha256(Path('/etc/hostname').read_bytes()).hexdigest()}  /etc/hostname\n"
    checksum.write_text(original + absolute, encoding="utf-8")
    with pytest.raises(ScenarioError) as excinfo:
        _verify_bundle_checksums(bundle)
    assert excinfo.value.code == "replay_untrusted"

    checksum.write_text(original + "0" * 64 + "  ../outside.bin\n", encoding="utf-8")
    with pytest.raises(ScenarioError) as excinfo:
        _verify_bundle_checksums(bundle)
    assert excinfo.value.code == "replay_untrusted"

    uncovered = "\n".join(
        line for line in original.splitlines() if not line.endswith("  replay/events.jsonl")
    ) + "\n"
    checksum.write_text(uncovered, encoding="utf-8")
    with pytest.raises(ScenarioError) as excinfo:
        _verify_bundle_checksums(bundle)
    assert excinfo.value.code == "replay_untrusted"

    checksum.write_text(original, encoding="utf-8")
    (bundle / "evil-link").symlink_to("/etc/hostname")
    with pytest.raises(ScenarioError) as excinfo:
        _verify_bundle_checksums(bundle)
    assert excinfo.value.code == "replay_untrusted"


def test_staging_copy_is_deadline_and_budget_aware(tmp_path: Path):
    source = tmp_path / "big.bin"
    source.write_bytes(b"x" * 10)
    with pytest.raises(ScenarioError) as excinfo:
        _copy_file_checked(source, tmp_path / "out.bin", deadline=time.monotonic() - 1)
    assert excinfo.value.code == "staging_timeout"
    with pytest.raises(ScenarioError) as excinfo:
        _copy_file_checked(source, tmp_path / "out2.bin", budget=4)
    assert excinfo.value.code == "staging_budget_exceeded"


def test_case_staging_budget_fails_the_case(tmp_path: Path):
    source = tmp_path / "big.bin"
    source.write_bytes(b"x" * 10)
    data = scenario_data(
        steps=[{"id": "status", "tool": "emu_status", "args": {}}],
        stage=[{"from": str(source), "to": "big.bin"}],
    )
    loaded = load_scenario(write(tmp_path, data))
    resolved = resolve_case(expand_matrix(loaded)[0], check_paths=True)
    backend = RecordingBackend()
    result = asyncio.run(
        CaseRun(
            resolved,
            run_id="budget-run",
            artifacts_root=tmp_path / "artifacts",
            backend_factory=lambda: backend,
            max_case_bytes=4,
        ).run()
    )
    assert result["status"] == "failed"
    assert result["failure"]["error"] == "staging_budget_exceeded"


def test_teardown_uses_one_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    class SlowStopBackend(RecordingBackend):
        async def call_tool(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
            if name == "emu_stop":
                await asyncio.sleep(10)
            return await super().call_tool(name, args)

    monkeypatch.setattr(scenario_module, "MAX_TEARDOWN_SECONDS", 0.2)
    data = scenario_data(
        steps=[{"id": "status", "tool": "emu_status", "args": {}}],
        teardown=[
            {"id": "stop-one", "tool": "emu_stop", "args": {}, "timeout_ms": 5000},
            {"id": "stop-two", "tool": "emu_stop", "args": {}, "timeout_ms": 5000},
        ],
    )
    started = time.monotonic()
    result, _ = run_engine(tmp_path, data, backend=SlowStopBackend())
    assert time.monotonic() - started < 3
    assert any(
        item.get("error") == "teardown_timeout"
        for item in (result.cases[0].get("secondary_errors") or [])
    )


def test_run_budget_counts_report_junit_and_annotations(tmp_path: Path):
    result, _ = run_engine(tmp_path, scenario_data())
    run_root = Path(result.artifacts)
    total = sum(
        (run_root / name).stat().st_size
        for name in ("report.json", "junit.xml", "annotations.json")
    )
    used = result.report["environment"]["run_budget"]["used_bytes"]
    assert used >= total


def test_corpus_stage_rejects_repository_workspace(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.bin").write_bytes(b"data")
    repo_workspace = Settings.load().repo_root / "p6-forbidden-stage"
    with pytest.raises(ValueError):
        stage_files(source, ["file.bin"], repo_workspace)
    assert not repo_workspace.exists()


def test_run_prg_rejects_protected_workspace(tmp_path: Path):
    from hatari_mcp.tools import build

    class FakeServer:
        def __init__(self) -> None:
            self.tools: dict[str, Any] = {}

        def tool(self, **kwargs: Any):
            def decorator(function):
                self.tools[kwargs["name"]] = function
                return function

            return decorator

    class FakeApp:
        async def start(self, **kwargs: Any):  # pragma: no cover - must not run
            raise AssertionError("app.start must not be called for a protected workspace")

    server = FakeServer()
    build.register(server, FakeApp())
    repo = Settings.load().repo_root
    program = repo / "mcp" / "project" / "examples" / "scenarios" / "boot-capture-stop.json"
    with pytest.raises(ToolError) as excinfo:
        asyncio.run(server.tools["run_prg"](str(program), workspace=str(repo)))
    assert "corpus_write_risk" in str(excinfo.value)


def test_benign_hatari_options_remain_allowed():
    args = [
        "--fast-boot",
        "on",
        "--fast-forward",
        "off",
        "--sound",
        "off",
        "--memsize",
        "1024",
        "--rtc-year",
        "2020",
        "--timer-d",
        "on",
        "--fastfdc",
        "off",
        "--cpu-exact",
        "on",
        "--monitor",
        "rgb",
        "--statusbar",
        "off",
    ]
    loaded = parse_scenario(scenario_data(config={"args": args}))
    assert loaded.config["args"] == args


def test_resolution_failure_still_redacts_secrets_from_run_files(tmp_path: Path):
    secret = "CONFIG-SECRET-TOKEN-1234"
    data = scenario_data(
        requires={"env": ["CONFIG_SECRET"]},
        config={"tos": "/etc/${CONFIG_SECRET}"},
    )
    path = write(tmp_path, data)
    engine = ScenarioEngine(
        backend_factory=lambda: RecordingBackend(),
        run_id="resolve-redact-run",
        environment={"CONFIG_SECRET": secret},
    )
    result = asyncio.run(engine.run(path, artifacts_root=tmp_path / "artifacts"))

    assert result.cases[0]["status"] == "error"
    assert result.cases[0]["failure"]["error"] == "path_not_allowed"
    assert secret not in json.dumps(result.cases[0])
    run_root = Path(result.artifacts)
    for name in ("report.json", "junit.xml", "annotations.json"):
        assert secret not in (run_root / name).read_text(encoding="utf-8")
    assert "<redacted>" in (run_root / "report.json").read_text(encoding="utf-8")


def test_capture_path_secrets_do_not_reach_manifest_or_checksums(tmp_path: Path):
    secret = "ARTIFACT-SECRET-9876"
    data = scenario_data(
        requires={"env": ["CAPTURE_SECRET"]},
        steps=[
            {
                "id": "status",
                "tool": "emu_status",
                "args": {},
                "capture": {"kind": "result", "path": "results/${CAPTURE_SECRET}.json"},
            }
        ],
    )
    path = write(tmp_path, data)
    engine = ScenarioEngine(
        backend_factory=lambda: RecordingBackend(),
        run_id="artifact-redact-run",
        environment={"CAPTURE_SECRET": secret},
    )
    result = asyncio.run(engine.run(path, artifacts_root=tmp_path / "artifacts"))
    assert result.exit_code == 0

    bundle = Path(result.artifacts) / str(result.cases[0]["artifacts"])
    manifest_text = (bundle / "manifest.json").read_text(encoding="utf-8")
    checksum_text = (bundle / "checksums.sha256").read_text(encoding="utf-8")
    assert secret not in manifest_text
    assert secret not in checksum_text
    assert not any(secret in item.name for item in bundle.rglob("*"))
    manifest = json.loads(manifest_text)
    assert any(item["path"] == "results/<redacted>.json" for item in manifest["artifacts"])
    assert (bundle / "results" / "<redacted>.json").is_file()


def test_run_prg_step_workspaces_are_confined_to_the_runner(tmp_path: Path):
    repo = Settings.load().repo_root
    cases = [
        ("workspace", str(repo), "path_not_allowed"),
        ("workspace", "/tmp", "path_not_allowed"),
    ]
    non_empty = tmp_path / "non-empty-workspace"
    non_empty.mkdir()
    (non_empty / "host-file").write_text("data", encoding="utf-8")
    cases.append(("workspace", str(non_empty), "path_not_allowed"))
    for _key, workspace, code in cases:
        data = scenario_data(
            steps=[{"id": "run", "tool": "run_prg", "args": {"program": "PROGRAM.PRG", "workspace": workspace}}]
        )
        loaded = parse_scenario(data, source=tmp_path / "scenario.json")
        with pytest.raises(ScenarioError) as excinfo:
            resolve_case(expand_matrix(loaded)[0], check_paths=True)
        assert excinfo.value.code == code, workspace

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    data = scenario_data(
        requires={"env": ["OKAMI_ROOT"]},
        steps=[
            {
                "id": "run",
                "tool": "run_prg",
                "args": {"program": "PROGRAM.PRG", "workspace": "${OKAMI_ROOT}"},
            }
        ],
    )
    loaded = parse_scenario(data, source=tmp_path / "scenario.json")
    with pytest.raises(ScenarioError) as excinfo:
        resolve_case(
            expand_matrix(loaded)[0],
            environment={"OKAMI_ROOT": str(corpus)},
            check_paths=True,
        )
    assert excinfo.value.code == "corpus_write_risk"


def test_run_prg_uses_the_runner_workspace_when_omitted(tmp_path: Path):
    program = tmp_path / "PROGRAM.PRG"
    program.write_bytes(b"prg")
    data = scenario_data(
        stage=[{"from": str(program), "to": "PROGRAM.PRG"}],
        steps=[{"id": "run", "tool": "run_prg", "args": {"program": "PROGRAM.PRG"}}],
    )
    result, backend = run_engine(tmp_path, data)
    assert result.exit_code == 0
    call = next(args for name, args in backend.calls if name == "run_prg")
    workspace = Path(call["workspace"])
    assert workspace.is_absolute()
    assert workspace != tmp_path
    assert Path(call["program"]) == (workspace / "PROGRAM.PRG").resolve()


def test_run_prg_rejects_a_non_runner_fresh_temp_workspace(tmp_path: Path):
    program = tmp_path / "PROGRAM.PRG"
    program.write_bytes(b"prg")
    other = tmp_path / "other-workspace"
    other.mkdir()
    data = scenario_data(
        steps=[
            {
                "id": "run",
                "tool": "run_prg",
                "args": {"program": str(program), "workspace": str(other)},
            }
        ]
    )
    result, backend = run_engine(tmp_path, data)
    assert result.cases[0]["status"] == "failed"
    assert result.cases[0]["failure"]["error"] == "path_not_allowed"
    assert not any(name == "run_prg" for name, _ in backend.calls)


def test_media_insert_rejects_corpus_images(tmp_path: Path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    image = corpus / "disk.st"
    image.write_bytes(b"\0" * 720 * 1024)
    data = scenario_data(
        requires={"env": ["OKAMI_ROOT"]},
        steps=[
            {
                "id": "flop",
                "tool": "media_insert_floppy",
                "args": {"drive": "A", "path": "${OKAMI_ROOT}/disk.st"},
            }
        ],
    )
    loaded = parse_scenario(data, source=tmp_path / "scenario.json")
    with pytest.raises(ScenarioError) as excinfo:
        resolve_case(
            expand_matrix(loaded)[0],
            environment={"OKAMI_ROOT": str(corpus)},
            check_paths=True,
        )
    assert excinfo.value.code == "corpus_write_risk"


def test_media_insert_rejects_images_outside_the_runner_workspace(tmp_path: Path):
    image = tmp_path / "disk.st"
    image.write_bytes(b"\0" * 720 * 1024)
    data = scenario_data(
        steps=[
            {
                "id": "flop",
                "tool": "media_insert_floppy",
                "args": {"drive": "A", "path": str(image)},
            }
        ]
    )
    result, backend = run_engine(tmp_path, data)
    assert result.cases[0]["status"] == "failed"
    assert result.cases[0]["failure"]["error"] == "path_not_allowed"
    assert not any(name == "media_insert_floppy" for name, _ in backend.calls)
