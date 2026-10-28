"""Manifest, checksum and user-outcome tests."""

from __future__ import annotations

import json
from pathlib import Path

from crash_detective import analysis


def _evidence(frame_path: Path) -> dict:
    return {
        "schema": 1,
        "run_id": "run-test-001",
        "created_at": 1700000000,
        "kind": "disk",
        "game": {"id": "game-1", "title": "Bundle Game", "path": "/tmp/game.zip"},
        "source": {
            "kind": "disk",
            "path": "/tmp/game.zip",
            "materialized": "/tmp/game.st",
            "zip_members": ["Disk.st"],
        },
        "launch": {"machine": "st", "tos": "/tmp/tos.img", "args": []},
        "result": {
            "ok": False,
            "status": "failure",
            "message": "guest exception",
            "failure_reason": "exception",
            "duration_ms": 500,
            "resolution": [832, 588],
        },
        "status": {"state": "at_prompt", "exception": {"type": "Bus Error"}},
        "timeline": [{"t_ms": 100, "state": "running", "nonblack_ratio": 0.4, "digest": "aa"}],
        "frames": [
            {
                "path": str(frame_path),
                "sha256": analysis.sha256_file(frame_path),
                "width": 16,
                "height": 16,
                "nonblack_ratio": 1.0,
            }
        ],
        "logs": {
            "stderr": ["INFO : start", "WARN : Bus Error reading at address $ffff8240"],
            "console": ["guest console line"],
        },
        "debug": {
            "registers": {"registers": {"pc": 0x1382A}},
            "pc": 0x1382A,
            "disassembly": {"instructions": ["0x0001382a: 4258  clr.w (a0)+"]},
            "stack": {"hex_words": ["0x00000000", "0x00001382"]},
            "backtrace": {"lines": ["#0  0x1382a"]},
            "errors": [],
        },
    }


def test_bundle_manifest_and_checksums(tmp_path: Path, bmp_factory) -> None:
    frame = bmp_factory("white.bmp", rgb=(240, 240, 240))
    evidence = _evidence(frame)
    classification = analysis.classify(evidence)
    hypotheses = analysis.diagnose(evidence, classification)

    bundle = analysis.write_bundle(tmp_path, evidence, classification, hypotheses)
    assert bundle == tmp_path / "runs" / "run-test-001" / "bundle"
    for relative in (
        "report.md",
        "report.json",
        "README.txt",
        "manifest.json",
        "logs/stderr.log",
        "logs/console.log",
        "debug/debug.json",
        "debug/disassembly.txt",
    ):
        assert (bundle / relative).is_file(), relative
    assert len(list((bundle / "screenshots").glob("*.bmp"))) == 1

    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    listed = {item["path"]: item for item in manifest["files"]}
    assert "manifest.json" not in listed
    assert "report.json" in listed
    assert "screenshots/frame-001.bmp" in listed
    for relative, item in listed.items():
        target = bundle / relative
        assert target.stat().st_size == item["size"]
        assert analysis.sha256_file(target) == item["sha256"]

    verification = analysis.verify_bundle(bundle)
    assert verification["ok"], verification["errors"]
    assert verification["files"] == len(listed)

    report = json.loads((bundle / "report.json").read_text(encoding="utf-8"))
    assert report["classification"]["category"] == "exception"
    assert report["hypotheses"][0]["id"] == "boot_crash"
    assert report["bundle"]["screenshots"][0]["sha256"] == analysis.sha256_file(frame)

    # Tampering is detected by checksum verification.
    victim = bundle / "screenshots" / "frame-001.bmp"
    original = victim.read_bytes()
    victim.write_bytes(original[:-1])
    broken = analysis.verify_bundle(bundle)
    assert not broken["ok"]
    assert any("size mismatch" in item or "checksum mismatch" in item for item in broken["errors"])


def test_user_outcome_persists(tmp_path: Path, bmp_factory) -> None:
    frame = bmp_factory()
    evidence = _evidence(frame)
    classification = analysis.classify(evidence)
    hypotheses = analysis.diagnose(evidence, classification)
    bundle = analysis.write_bundle(tmp_path, evidence, classification, hypotheses)

    entry = analysis.record_outcome(
        tmp_path,
        "run-test-001",
        "game-1",
        "failure",
        title="Bundle Game",
        note="detected=exception",
        bundle=str(bundle),
        detected="exception",
    )
    assert entry["outcome"] == "failure"
    assert entry["bundle"] == str(bundle)

    stored = analysis.load_outcomes(tmp_path)
    assert stored["runs"]["run-test-001"]["outcome"] == "failure"
    assert stored["runs"]["run-test-001"]["detected"] == "exception"
    assert analysis.outcomes_path(tmp_path).is_file()

    # A second mark for the same run replaces the previous outcome.
    analysis.record_outcome(tmp_path, "run-test-001", "game-1", "success")
    stored = analysis.load_outcomes(tmp_path)
    assert stored["runs"]["run-test-001"]["outcome"] == "success"
