from __future__ import annotations

import json

import pytest

from hatari_mcp.services.scenario import (
    ScenarioError,
    expand_matrix,
    load_scenario,
    parse_scenario,
    resolve_case,
    validate_scenario,
)


def scenario(**overrides):
    data = {
        "version": 1,
        "id": "demo",
        "name": "Demo",
        "requires": {"env": ["TOS_PATH"]},
        "determinism": "tolerant",
        "timeouts": {"scenario_ms": 5000, "step_ms": 1000},
        "config": {"profile": "st", "tos": "${TOS_PATH}"},
        "steps": [
            {"id": "start", "tool": "emu_start", "args": {}},
            {"id": "stop", "tool": "emu_stop", "args": {}},
        ],
    }
    data.update(overrides)
    return data


def write(tmp_path, data):
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_minimal_scenario_and_case_id(tmp_path):
    path = write(tmp_path, scenario())
    loaded = load_scenario(path, environment={"TOS_PATH": "/tmp/tos.img"})

    assert loaded.id == "demo"
    assert loaded.determinism == "tolerant"
    cases = expand_matrix(loaded)
    assert [case.id for case in cases] == ["demo"]
    resolved = resolve_case(cases[0], environment={"TOS_PATH": "/tmp/tos.img"}, check_paths=False)
    assert resolved.config["tos"] == "/tmp/tos.img"
    assert len(resolved.resolved_hash) == 64


def test_unknown_keys_are_errors(tmp_path):
    data = scenario(unexpected=True)
    with pytest.raises(ScenarioError) as excinfo:
        parse_scenario(data)
    assert excinfo.value.code == "schema_error"

    data = scenario()
    data["steps"][0]["mystery"] = 1
    with pytest.raises(ScenarioError) as excinfo:
        parse_scenario(data)
    assert "Unknown" in excinfo.value.message


def test_duplicate_step_ids_rejected():
    data = scenario()
    data["steps"].append({"id": "start", "tool": "emu_status", "args": {}})
    with pytest.raises(ScenarioError) as excinfo:
        parse_scenario(data)
    assert excinfo.value.code == "duplicate_step_id"


def test_undeclared_variable_rejected():
    data = scenario()
    data["config"]["tos"] = "${NOT_DECLARED}"
    with pytest.raises(ScenarioError) as excinfo:
        parse_scenario(data)
    assert excinfo.value.code == "undefined_variable"


def test_declared_variable_expansion_is_single_pass():
    data = scenario()
    data["vars"] = {"ROOT": "${TOS_PATH}"}
    loaded = parse_scenario(data)
    case = expand_matrix(loaded)[0]
    resolved = resolve_case(case, environment={"TOS_PATH": "${INNER}"}, check_paths=False)
    # Single pass: the value of TOS_PATH is not expanded again.
    assert resolved.config["tos"] == "${INNER}"


def test_matrix_is_deterministic_and_overrides_apply():
    data = scenario()
    data["matrix"] = {"machine": ["ste", "st"], "tos": ["emutos"]}
    data["config"] = {"profile": "${machine}", "tos": "${TOS_PATH}"}
    data["overrides"] = [
        {"match": {"machine": "st"}, "timeouts": {"step_ms": 250}, "config": {"write_protect": "auto"}}
    ]
    loaded = parse_scenario(data)
    cases = expand_matrix(loaded)

    assert [case.id for case in cases] == ["demo_machine_st_tos_emutos", "demo_machine_ste_tos_emutos"]
    st_case = next(case for case in cases if case.axes["machine"] == "st")
    resolved = resolve_case(st_case, environment={"TOS_PATH": "/tmp/tos.img"}, check_paths=False)
    assert resolved.config["profile"] == "st"
    assert resolved.config["write_protect"] == "auto"
    assert resolved.timeouts["step_ms"] == 250


def test_max_cases_bounds_matrix():
    data = scenario()
    data["matrix"] = {"a": [1, 2, 3]}
    loaded = parse_scenario(data)
    with pytest.raises(ScenarioError) as excinfo:
        expand_matrix(loaded, max_cases=2)
    assert excinfo.value.code == "matrix_too_large"


def test_path_traversal_and_absolute_escape_rejected(tmp_path):
    source = tmp_path / "scenario.json"
    source.write_text("{}", encoding="utf-8")
    data = scenario()
    data["config"]["workspace"] = "../outside"
    loaded = parse_scenario(data, source=source.resolve())
    with pytest.raises(ScenarioError) as excinfo:
        resolve_case(expand_matrix(loaded)[0], environment={"TOS_PATH": "/tmp/tos.img"}, check_paths=True)
    assert excinfo.value.code == "path_not_allowed"

    data = scenario()
    data["steps"][0]["args"] = {"workspace": "/etc"}
    loaded = parse_scenario(data, source=source.resolve())
    with pytest.raises(ScenarioError) as excinfo:
        resolve_case(expand_matrix(loaded)[0], environment={"TOS_PATH": "/tmp/tos.img"}, check_paths=True)
    assert excinfo.value.code == "path_not_allowed"


def test_unsafe_hatari_args_rejected():
    data = scenario()
    data["config"]["args"] = ["--control-socket", "/tmp/hijack.sock"]
    with pytest.raises(ScenarioError) as excinfo:
        parse_scenario(data)
    assert excinfo.value.code == "unsafe_argument"


def test_validation_returns_case_hashes(tmp_path):
    path = write(tmp_path, scenario())
    result = validate_scenario(path, environment={"TOS_PATH": "/tmp/tos.img"})
    assert result["ok"] is True
    assert result["cases"] == 1
    assert result["resolved_cases"][0]["hash"]


def test_requires_skip_reason_and_timeout_validation():
    data = scenario()
    data["requires"] = {"env": ["TOS_PATH"], "skip_reason": "TOS missing"}
    loaded = parse_scenario(data)
    assert loaded.skip_reason == "TOS missing"

    data = scenario(timeouts={"scenario_ms": 10, "step_ms": 20})
    loaded = parse_scenario(data)
    assert loaded.timeouts["step_ms"] == 10


def test_path_traversal_is_rejected_even_when_declared_env_is_missing(tmp_path):
    source = tmp_path / "scenario.json"
    source.write_text("{}", encoding="utf-8")
    data = scenario()
    data["config"]["workspace"] = "../outside"
    loaded = parse_scenario(data, source=source.resolve())
    with pytest.raises(ScenarioError) as excinfo:
        resolve_case(expand_matrix(loaded)[0], environment={}, check_paths=True)
    assert excinfo.value.code == "path_not_allowed"
