from __future__ import annotations

import os
from pathlib import Path

import pytest

from hatari_mcp.corpus import discover_corpus
from hatari_mcp.services.scenario import validate_scenario

SCENARIOS = Path(__file__).resolve().parents[2] / "examples" / "scenarios"
CASES = [
    ("okami", SCENARIOS / "okami-shell-boot.json"),
    ("freemint", SCENARIOS / "freemint-cflib-smoke.json"),
    ("godlib", SCENARIOS / "godlib-sample-smoke.json"),
]


@pytest.mark.parametrize(("project", "scenario"), CASES)
def test_corpus_scenarios_validate_or_skip(project, scenario):
    data = discover_corpus(os.environ)
    details = data["projects"][project]
    if not details["available"]:
        pytest.skip(details["skip_reason"] or f"{project} corpus is not available")
    result = validate_scenario(scenario, environment=os.environ)
    assert result["ok"] is True
    assert result["cases"] >= 1


def test_real_okami_staging_does_not_mutate_tree(tmp_path):
    from hatari_mcp.corpus import file_signature, stage_files

    data = discover_corpus(os.environ)
    details = data["projects"]["okami"]
    if not details["available"]:
        pytest.skip(details["skip_reason"] or "Okami-Shell is not available")
    root = Path(details["root"])
    source = root / "Files" / "GEM.PRG"
    if not source.is_file():
        pytest.skip("Okami-Shell Files/GEM.PRG is not present")
    before = file_signature(source)
    stage_files(root, [{"path": "Files/GEM.PRG", "to": "Files/GEM.PRG"}], tmp_path / "workspace")
    after = file_signature(source)
    assert before == after
    assert (tmp_path / "workspace" / "Files" / "GEM.PRG").is_file()
