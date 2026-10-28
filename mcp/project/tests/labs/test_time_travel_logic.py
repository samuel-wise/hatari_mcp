import hashlib
import os
from pathlib import Path

os.environ.setdefault("HATARI_MCP_LABS", "1")

from labs import time_travel_spike as tt  # noqa: E402


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _evidence(tmp_path: Path) -> dict:
    bundle = tmp_path / "input-replay"
    bundle.mkdir()
    checkpoints = [
        {
            "vbl": vbl,
            "screenshot_sha256": _sha(f"shot-{vbl}"),
            "memory_sha256": _sha(f"mem-{vbl}"),
            "expected_screenshot_sha256": _sha(f"shot-{vbl}"),
            "expected_memory_sha256": _sha(f"mem-{vbl}"),
            "matches": True,
        }
        for vbl in (150, 300, 450)
    ]
    return {
        "bundle_path": str(bundle),
        "checkpoints": checkpoints,
        "hashes": {"input_log_sha256": _sha("input-log")},
        "event_count": 1200,
        "replay_status": "matched",
    }


def test_snapshot_and_checkpoint_targets():
    assert tt.snapshot_targets(50, 500) == list(range(50, 501, 50))
    assert tt.checkpoint_targets(50, 500) == [150, 300, 450]
    assert tt.checkpoint_targets(50, 200) == [150]
    assert tt.checkpoint_targets(25, 500) == [75, 150, 225]


def test_cadence_estimates_match_retained_snapshot_size():
    estimates = tt.cadence_estimates(126881)
    assert estimates["25_vbl"]["snapshots"] == 120
    assert estimates["50_vbl"]["snapshots"] == 60
    assert estimates["100_vbl"]["snapshots"] == 30
    assert estimates["50_vbl"]["disk_mib"] == (126881 * 60) / (1024 * 1024)


def test_static_check_without_input_replay_is_not_go():
    summary = {
        "rewind_total_s_max": 0.88,
        "size_bytes_median": 126_881,
        "save_plus_load_s": 0.06,
        "all_screenshot_matches": True,
        "all_memory_matches": True,
        "all_replay_deterministic": True,
    }
    verdict = tt.rewind_verdict(summary)
    assert verdict["go"] is False
    assert verdict["go_cost"] is True
    assert verdict["static_only"] is True
    assert verdict["verdict"].startswith("INCONCLUSIVE")


def test_bare_input_replay_flag_is_not_evidence():
    summary = {
        "rewind_total_s_max": 0.88,
        "size_bytes_median": 126_881,
        "save_plus_load_s": 0.06,
        "all_screenshot_matches": True,
        "all_memory_matches": True,
        "all_replay_deterministic": True,
    }
    for bare in (True, "yes", {"input_replay_proven": True}):
        assert tt.validate_input_replay_evidence(bare)["ok"] is False
        verdict = tt.rewind_verdict(summary, input_replay_evidence=bare)
        assert verdict["go"] is False
        assert verdict["verdict"].startswith("INCONCLUSIVE")


def test_valid_input_replay_evidence_and_cost_are_required_for_go(tmp_path):
    summary = {
        "rewind_total_s_max": 0.88,
        "size_bytes_median": 126_881,
        "save_plus_load_s": 0.06,
        "all_screenshot_matches": True,
        "all_memory_matches": True,
        "all_replay_deterministic": True,
    }
    evidence = _evidence(tmp_path)
    result = tt.rewind_verdict(summary, input_replay_evidence=evidence)
    assert result["go"] is True
    assert result["input_replay_proven"] is True
    expensive = dict(summary, size_bytes_median=65 * 1024 * 1024)
    result = tt.rewind_verdict(expensive, input_replay_evidence=evidence)
    assert result["go"] is False
    assert result["verdict"].startswith("NO-GO")


def test_incomplete_evidence_is_rejected(tmp_path):
    summary = {
        "rewind_total_s_max": 0.88,
        "size_bytes_median": 126_881,
        "save_plus_load_s": 0.06,
        "all_screenshot_matches": True,
        "all_memory_matches": True,
        "all_replay_deterministic": True,
    }
    evidence = _evidence(tmp_path)
    evidence["event_count"] = 0
    assert tt.validate_input_replay_evidence(evidence)["ok"] is False
    assert tt.rewind_verdict(summary, input_replay_evidence=evidence)["go"] is False
