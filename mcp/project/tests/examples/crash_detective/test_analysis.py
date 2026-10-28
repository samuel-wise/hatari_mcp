"""Unit tests for evidence classification and hypothesis ranking."""

from __future__ import annotations

from crash_detective import analysis


def _base(**overrides):
    evidence = {
        "kind": "disk",
        "game": {"id": "game-1", "title": "Test Game", "path": "/tmp/test.st"},
        "source": {"kind": "disk", "path": "/tmp/test.st", "materialized": "/tmp/test.st"},
        "launch": {"machine": "st", "profile": "st", "tos": "/tmp/emutos.img", "args": []},
        "result": {
            "ok": True,
            "status": "success",
            "message": "visible frame",
            "failure_reason": None,
            "duration_ms": 1200,
            "resolution": [832, 588],
        },
        "status": {"state": "running", "exception": None},
        "frames": [],
        "timeline": [],
        "logs": {"stderr": [], "console": []},
        "debug": {},
    }
    evidence.update(overrides)
    return evidence


def test_classifies_visible_boot() -> None:
    evidence = _base(
        frames=[
            {
                "path": "/tmp/a.bmp",
                "sha256": "a" * 64,
                "width": 832,
                "height": 588,
                "nonblack_ratio": 0.42,
            }
        ],
        timeline=[{"t_ms": 900, "state": "running", "nonblack_ratio": 0.42, "digest": "abcd"}],
    )
    result = analysis.classify(evidence)
    assert result["booted"] is True
    assert result["category"] == "booted_visible"
    assert result["confidence"] >= 0.5
    assert any(signal["code"] == "visible_frame" for signal in result["signals"])
    assert analysis.diagnose(evidence, result) == []


def test_classifies_guest_exception() -> None:
    evidence = _base(
        result={
            "ok": False,
            "status": "failure",
            "message": "guest exception",
            "failure_reason": "exception",
            "duration_ms": 400,
        },
        status={
            "state": "at_prompt",
            "at_prompt": True,
            "exception": {"type": "Bus Error", "line": "WARN : Bus Error reading at address $ffff8240"},
        },
        logs={
            "stderr": ["WARN : Bus Error reading at address $ffff8240, PC=$1382a"],
            "console": [],
        },
        debug={"registers": {"registers": {"pc": 0x1382A}}, "pc": 0x1382A},
    )
    result = analysis.classify(evidence)
    assert result["booted"] is False
    assert result["category"] == "exception"
    assert result["confidence"] >= 0.9
    assert any(signal["code"] == "guest_exception" for signal in result["signals"])

    hypotheses = analysis.diagnose(evidence, result)
    assert hypotheses[0]["id"] == "boot_crash"
    assert hypotheses[0]["confidence"] >= 0.9
    assert any("0x0001382a" in item for item in hypotheses[0]["evidence"])


def test_classifies_blank_screen_and_no_frames() -> None:
    blank = _base(
        result={
            "ok": False,
            "status": "failure",
            "message": "boot timed out with a black screen",
            "failure_reason": "timeout_blank_screen",
            "duration_ms": 30000,
        },
        frames=[
            {
                "path": "/tmp/black.bmp",
                "sha256": "b" * 64,
                "width": 832,
                "height": 588,
                "nonblack_ratio": 0.0,
            }
        ],
        timeline=[{"t_ms": 1000, "state": "running", "nonblack_ratio": 0.0, "digest": "bbbb"}],
    )
    result = analysis.classify(blank)
    assert result["booted"] is False
    assert result["category"] == "blank_screen"
    hypotheses = analysis.diagnose(blank, result)
    assert hypotheses[0]["id"] == "blank_or_black_frame"
    assert all(0.0 < item["confidence"] <= 0.99 for item in hypotheses)

    no_frames = _base(
        result={
            "ok": False,
            "status": "failure",
            "message": "screen capture failed repeatedly",
            "failure_reason": "capture_failed",
            "duration_ms": 5000,
        },
        frames=[],
        timeline=[],
    )
    result = analysis.classify(no_frames)
    assert result["category"] == "capture_failed"
    assert any(signal["code"] == "capture_failed" for signal in result["signals"])


def test_classifies_stalled_boot_and_repeated_resets() -> None:
    stalled = _base(
        result={
            "ok": False,
            "status": "failure",
            "message": "boot timed out before a stable visible frame",
            "failure_reason": "timeout_no_stable_frame",
            "duration_ms": 30000,
        },
        frames=[
            {
                "path": "/tmp/still.bmp",
                "sha256": "c" * 64,
                "width": 832,
                "height": 588,
                "nonblack_ratio": 0.5,
            }
        ],
        timeline=[
            {"t_ms": 1000, "state": "running", "nonblack_ratio": 0.5, "digest": "same"},
            {"t_ms": 2000, "state": "running", "nonblack_ratio": 0.5, "digest": "same"},
        ],
    )
    result = analysis.classify(stalled)
    assert result["category"] == "stalled_boot"
    assert analysis.diagnose(stalled, result)[0]["id"] == "stalled_boot"

    resetting = _base(
        result={
            "ok": False,
            "status": "failure",
            "message": "boot loop",
            "failure_reason": None,
            "duration_ms": 20000,
        },
        status={"state": "running"},
        logs={
            "stderr": ["Cold reset requested", "Cold reset requested", "Cold reset requested"],
            "console": [],
        },
        frames=[],
    )
    result = analysis.classify(resetting)
    assert result["category"] == "repeated_resets"
    assert result["observations"]["reset_lines"] == 3


def test_classifies_disk_error_and_emulator_exit() -> None:
    disk = _base(
        result={
            "ok": False,
            "status": "failure",
            "message": "boot failed",
            "failure_reason": None,
            "duration_ms": 5000,
        },
        status={"state": "running"},
        logs={"stderr": ["GEMDOS: insert disk in drive A"], "console": []},
        frames=[],
    )
    result = analysis.classify(disk)
    assert result["category"] == "disk_error"
    ids = [item["id"] for item in analysis.diagnose(disk, result)]
    assert "missing_or_unreadable_disk" in ids

    exited = _base(
        result={
            "ok": False,
            "status": "failure",
            "message": "emulator left running state",
            "failure_reason": "process_exit",
            "duration_ms": 500,
        },
        status={"state": "crashed", "returncode": 1, "exit_reason": "process_exit"},
        frames=[],
    )
    result = analysis.classify(exited)
    assert result["category"] == "emulator_exit"
    assert any(item["id"] == "emulator_exit" for item in analysis.diagnose(exited, result))


def test_hypothesis_ranking_and_confidence_bounds() -> None:
    exception = _base(
        result={
            "ok": False,
            "status": "failure",
            "message": "guest exception",
            "failure_reason": "exception",
            "duration_ms": 300,
        },
        launch={"machine": "ste", "profile": "ste", "tos": "/tmp/tos104.img", "args": []},
        status={"state": "at_prompt", "exception": {"type": "Illegal instruction"}},
        logs={"stderr": ["Illegal instruction at $1234"], "console": []},
        frames=[],
    )
    classification = analysis.classify(exception)
    hypotheses = analysis.diagnose(exception, classification)
    assert hypotheses
    assert hypotheses[0]["id"] == "boot_crash"
    assert hypotheses == sorted(hypotheses, key=lambda item: (-item["confidence"], item["id"]))
    assert all(0.0 < item["confidence"] <= 0.99 for item in hypotheses)
    assert all(item["evidence"] for item in hypotheses)
    assert any(item["id"] == "unsupported_tos_machine" for item in hypotheses)
    assert hypotheses[0]["confidence"] > next(
        item["confidence"] for item in hypotheses if item["id"] == "unsupported_tos_machine"
    )

    undetermined = _base(
        result={
            "ok": False,
            "status": "failure",
            "message": "odd failure",
            "failure_reason": None,
            "duration_ms": 1,
        },
        launch={},
        status={"state": "running"},
        frames=[
            {
                "path": "/tmp/visible.bmp",
                "sha256": "d" * 64,
                "width": 832,
                "height": 588,
                "nonblack_ratio": 0.5,
            }
        ],
        logs={"stderr": [], "console": []},
    )
    classification = {"category": "unknown", "booted": None, "confidence": 0.2}
    hypotheses = analysis.diagnose(undetermined, classification)
    assert hypotheses[0]["id"] == "undetermined"


def test_bmp_sampling_and_report(bmp_factory) -> None:
    black = bmp_factory("black.bmp", width=16, height=16, rgb=(0, 0, 0))
    white = bmp_factory("white.bmp", width=16, height=16, rgb=(255, 255, 255))
    assert analysis.bmp_stats(black)["nonblack_ratio"] == 0.0
    assert analysis.bmp_stats(white)["nonblack_ratio"] == 1.0
    ppm = analysis.bmp_to_ppm_bytes(white)
    assert ppm.startswith(b"P6\n16 16\n255\n")

    evidence = _base(
        result={
            "ok": False,
            "status": "failure",
            "message": "guest exception",
            "failure_reason": "exception",
            "duration_ms": 300,
        },
        status={"state": "at_prompt", "exception": {"type": "Illegal instruction"}},
        frames=[],
    )
    classification = analysis.classify(evidence)
    hypotheses = analysis.diagnose(evidence, classification)
    report = analysis.build_report(evidence, classification, hypotheses)
    assert "Hypotheses (not confirmed)" in report
    assert "not confirmed root causes" in report
    assert hypotheses[0]["title"] in report
