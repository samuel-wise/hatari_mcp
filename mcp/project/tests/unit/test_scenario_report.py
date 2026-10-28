from __future__ import annotations

import json
import xml.etree.ElementTree as ET

from hatari_mcp.scenario_cli import main
from hatari_mcp.services.scenario import (
    EXIT_FAILED,
    EXIT_INTERNAL,
    EXIT_OK,
    EXIT_WATCHDOG,
    build_annotations,
    build_junit_xml,
    build_report,
    exit_code_for,
    summarize,
)


def cases():
    return [
        {"case": "ok", "scenario": "s", "status": "passed", "duration_ms": 10},
        {
            "case": "bad",
            "scenario": "s",
            "status": "failed",
            "duration_ms": 20,
            "failure": {"error": "step_timeout", "step_id": "wait", "message": "too slow"},
        },
        {"case": "skip", "scenario": "s", "status": "skipped", "duration_ms": 1, "failure": {"message": "no root"}},
        {"case": "err", "scenario": "s", "status": "error", "duration_ms": 2, "failure": {"error": "internal"}},
    ]


def test_summary_and_exit_codes():
    report = build_report(
        run_id="r",
        started_utc="a",
        finished_utc="b",
        cases=cases(),
    )
    assert summarize(cases()) == {"passed": 1, "failed": 1, "skipped": 1, "error": 1, "cancelled": 0}
    assert exit_code_for(report["summary"]) == EXIT_FAILED
    assert exit_code_for({"passed": 1, "failed": 0, "skipped": 1, "error": 0, "cancelled": 0}) == EXIT_OK
    assert (
        exit_code_for(
            {"passed": 0, "failed": 0, "skipped": 1, "error": 0, "cancelled": 0},
            fail_on_skip=True,
        )
        == EXIT_FAILED
    )
    assert exit_code_for({"error": 1}, watchdog=True) == EXIT_WATCHDOG
    assert exit_code_for({"error": 1}) == EXIT_INTERNAL


def test_junit_xml_is_well_formed_and_distinguishes_statuses():
    report = build_report(run_id="r", started_utc="a", finished_utc="b", cases=cases())
    xml = build_junit_xml(report)
    root = ET.fromstring(xml)
    assert root.tag == "testsuite"
    assert root.attrib["tests"] == "4"
    assert root.attrib["failures"] == "1"
    assert root.attrib["errors"] == "1"
    assert root.attrib["skipped"] == "1"
    testcases = {case.attrib["name"]: case for case in root.findall("testcase")}
    assert testcases["skip"].find("skipped") is not None
    failure = testcases["bad"].find("failure")
    assert failure is not None
    assert "wait" in failure.text
    assert testcases["err"].find("error") is not None
    props = {prop.attrib["name"]: prop.attrib["value"] for prop in root.findall("properties/property")}
    assert props["run_id"] == "r"


def test_annotations_only_failures_and_errors():
    report = build_report(run_id="r", started_utc="a", finished_utc="b", cases=cases())
    annotations = build_annotations(report)
    assert len(annotations) == 2
    assert {item["level"] for item in annotations} == {"warning", "error"}


def test_cli_validate_rejects_bad_scenario_and_returns_usage_code(tmp_path, capsys):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"version": 99}), encoding="utf-8")
    code = main(["validate", str(path), "--json"])
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "schema_error"
