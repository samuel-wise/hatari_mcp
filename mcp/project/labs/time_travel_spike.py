"""P8.1 snapshot-and-replay time-travel spike.

Measures stock ``statesave``/``stateload`` size and latency at a fixed VBL
cadence, then rewinds to three checkpoints from the nearest earlier snapshot
and compares a screenshot hash plus a RAM region against the forward run.

This is bounded snapshot+replay, not instruction-level reverse execution. The
workload has no guest input, so the replay check is static. The overall verdict
is INCONCLUSIVE until P7.5 frame-locked input replay is actually tested.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any

from . import require_labs
from .harness import Hatari, make_out_dir, sha256_file, write_json

require_labs()

MEM_ADDRESS = 0x10000
MEM_SIZE = 0x4000
CHECKPOINT_MULTIPLES = (3, 6, 9)
HASH_RE = re.compile(r"^[0-9a-fA-F]{64}$")
REPLAY_STATUSES = {"matched", "match", "pass", "passed", "ok", "deterministic"}


def snapshot_targets(cadence: int, last_vbl: int) -> list[int]:
    if cadence <= 0:
        raise ValueError("cadence must be positive")
    return list(range(cadence, last_vbl + 1, cadence))


def checkpoint_targets(cadence: int, last_vbl: int) -> list[int]:
    if cadence <= 0:
        raise ValueError("cadence must be positive")
    return sorted(item for item in (cadence * multiple for multiple in CHECKPOINT_MULTIPLES) if item <= last_vbl)


def cadence_estimates(median_size: int, *, seconds: int = 60) -> dict[str, dict[str, float | int]]:
    estimates: dict[str, dict[str, float | int]] = {}
    for cadence in (25, 50, 100):
        count = seconds * 50 // cadence
        estimates[f"{cadence}_vbl"] = {
            "snapshots": count,
            "disk_mib": median_size * count / (1024 * 1024),
        }
    return estimates


def validate_input_replay_evidence(evidence: Any) -> dict[str, Any]:
    """Validate an input-replay evidence record.

    A bare boolean or CLI flag is never proof. The record must name a bundle
    path, at least three checkpoints with matching screenshot and memory
    hashes, a positive event count, hashes, and a matched replay status.
    """
    checks: dict[str, bool] = {}
    reasons: list[str] = []
    if not isinstance(evidence, dict):
        return {
            "ok": False,
            "proven": False,
            "checks": {"evidence_object": False},
            "reasons": ["missing input-replay evidence object; a bare flag is insufficient"],
            "checkpoint_count": 0,
            "event_count": None,
            "replay_status": None,
            "bundle_path": None,
        }
    checks["evidence_object"] = True
    bundle = evidence.get("bundle_path")
    checks["bundle_path_present"] = bool(bundle)
    bundle_exists = bool(bundle) and Path(str(bundle)).expanduser().exists()
    checks["bundle_path_exists"] = bundle_exists
    if not bundle:
        reasons.append("bundle_path is missing")
    elif not bundle_exists:
        reasons.append(f"bundle_path does not exist: {bundle}")

    status = str(evidence.get("replay_status", "")).lower()
    checks["replay_status_matched"] = status in REPLAY_STATUSES
    if not checks["replay_status_matched"]:
        reasons.append("replay_status is not a matched/passed value")

    event_count = evidence.get("event_count")
    try:
        event_count_value = int(event_count)
    except (TypeError, ValueError):
        event_count_value = 0
    checks["event_count_positive"] = event_count_value > 0
    if not checks["event_count_positive"]:
        reasons.append("event_count must be a positive integer")

    hashes = evidence.get("hashes")
    hash_values = (
        [value for value in hashes.values() if isinstance(value, str)]
        if isinstance(hashes, dict)
        else []
    )
    checks["hashes_present"] = bool(hash_values) and any(
        HASH_RE.match(value) for value in hash_values
    )
    if not checks["hashes_present"]:
        reasons.append("hashes must be a non-empty object containing at least one SHA-256")

    checkpoints = evidence.get("checkpoints")
    checkpoint_count = len(checkpoints) if isinstance(checkpoints, list) else 0
    checks["checkpoints_present"] = checkpoint_count >= 3
    if checkpoint_count < 3:
        reasons.append("at least three input-replay checkpoints are required")
    checkpoints_ok = checkpoint_count >= 3
    if checkpoints_ok:
        for index, checkpoint in enumerate(checkpoints):
            if not isinstance(checkpoint, dict):
                checkpoints_ok = False
                reasons.append(f"checkpoint {index} is not an object")
                continue
            if checkpoint.get("vbl") is None:
                checkpoints_ok = False
                reasons.append(f"checkpoint {index} has no vbl")
            screenshot = checkpoint.get("screenshot_sha256")
            memory = checkpoint.get("memory_sha256")
            expected_screenshot = checkpoint.get("expected_screenshot_sha256")
            expected_memory = checkpoint.get("expected_memory_sha256")
            hashes_ok = bool(
                isinstance(screenshot, str)
                and HASH_RE.match(screenshot)
                and isinstance(memory, str)
                and HASH_RE.match(memory)
            )
            expected_ok = bool(
                (expected_screenshot is None or expected_screenshot == screenshot)
                and (expected_memory is None or expected_memory == memory)
            )
            matches = checkpoint.get("matches")
            self_consistent = bool(matches is True or (expected_screenshot and expected_memory))
            if not (hashes_ok and expected_ok and self_consistent):
                checkpoints_ok = False
                reasons.append(
                    f"checkpoint {index} lacks valid matching screenshot/memory hashes"
                )
    checks["checkpoints_match"] = checkpoints_ok

    ok = all(checks.values())
    return {
        "ok": ok,
        "proven": ok,
        "checks": checks,
        "reasons": reasons,
        "checkpoint_count": checkpoint_count,
        "event_count": event_count_value if event_count is not None else None,
        "replay_status": status or None,
        "bundle_path": bundle,
    }


def rewind_verdict(
    summary: dict[str, Any], *, input_replay_evidence: Any = None
) -> dict[str, Any]:
    cost_ok = bool(
        summary.get("rewind_total_s_max", float("inf")) < 2.0
        and summary.get("size_bytes_median", 1 << 60) < 64 * 1024 * 1024
        and summary.get("save_plus_load_s", float("inf")) < 2.0
    )
    static_match = bool(
        summary.get("all_screenshot_matches")
        and summary.get("all_memory_matches")
        and summary.get("all_replay_deterministic")
    )
    evidence = validate_input_replay_evidence(input_replay_evidence)
    input_replay_proven = evidence["ok"]
    # Snapshot cost can be GO on its own. Exact replay cannot be GO on a
    # static, input-free workload or a bare flag.
    overall = bool(cost_ok and static_match and input_replay_proven)
    if overall:
        verdict = "GO: snapshot cost and evidenced input-driven replay both pass"
    elif cost_ok and static_match and not input_replay_proven:
        verdict = (
            "INCONCLUSIVE: snapshot cost is acceptable and the static checkpoint "
            "comparison matches, but valid input-replay evidence was not supplied. "
            "This is not time-travel GO."
        )
    elif not cost_ok:
        verdict = "NO-GO: snapshot size or save/load latency exceeds the declared budget"
    else:
        verdict = "NO-GO: static forward/replay checkpoints diverged"
    return {
        "go_cost": cost_ok,
        "go_static_checkpoints": static_match,
        "input_replay_proven": input_replay_proven,
        "input_replay_evidence": evidence,
        "static_only": not input_replay_proven,
        "go": overall,
        "verdict": verdict,
    }


def run(
    out_dir: Path,
    *,
    profile: str = "st",
    cadence: int = 50,
    last_vbl: int = 500,
    mem_address: int = MEM_ADDRESS,
    mem_size: int = MEM_SIZE,
    input_replay_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    work = out_dir / "states"
    work.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "spike": "P8.1",
        "profile": profile,
        "cadence_vbl": cadence,
        "last_vbl": last_vbl,
        "memory_address": mem_address,
        "memory_size": mem_size,
        "snapshots": [],
        "checkpoints": [],
        "forward_reference": {},
        "rewinds": [],
        "replay_determinism": [],
        "limitations": [
            "Stock Hatari has no frame-locked input (P7.5); this run uses no guest input.",
            "The checkpoint comparison is static and therefore insufficient for input replay.",
            "Host-side disk/media writes are not reverted by stateload.",
            "Rewind is bounded by snapshots and replay, not reverse execution.",
        ],
    }
    checkpoints = set(checkpoint_targets(cadence, last_vbl))
    with Hatari(profile) as emu:
        emu.start()
        for target in snapshot_targets(cadence, last_vbl):
            actual = emu.wait_vbl(target)
            state = work / f"state_{target:05d}.vbl"
            save_s = emu.statesave(state)
            size = state.stat().st_size
            result["snapshots"].append(
                {
                    "requested_vbl": target,
                    "actual_vbl": actual,
                    "path": str(state),
                    "size_bytes": size,
                    "save_s": save_s,
                }
            )
            if target in checkpoints:
                shot = work / f"ref_{target:05d}.png"
                shot_s = emu.screenshot(shot)
                mem = work / f"ref_{target:05d}.bin"
                emu.savebin(mem, mem_address, mem_size)
                result["forward_reference"][str(target)] = {
                    "vbl": actual,
                    "screenshot_sha256": sha256_file(shot),
                    "memory_sha256": sha256_file(mem),
                    "screenshot_s": shot_s,
                }

        for checkpoint in sorted(checkpoints):
            state_target = checkpoint - cadence
            state = work / f"state_{state_target:05d}.vbl"
            snapshot = next(
                item for item in result["snapshots"] if item["requested_vbl"] == state_target
            )
            reference = result["forward_reference"][str(checkpoint)]
            target_vbl = int(reference["vbl"])
            start = time.perf_counter()
            load = emu.stateload_observable(state, expected_vbl=int(snapshot["actual_vbl"]))
            replayed_vbl = emu.wait_vbl(target_vbl)
            shot = work / f"rewind_{checkpoint:05d}.png"
            emu.screenshot(shot)
            mem = work / f"rewind_{checkpoint:05d}.bin"
            emu.savebin(mem, mem_address, mem_size)
            total_s = time.perf_counter() - start
            result["rewinds"].append(
                {
                    "checkpoint_vbl": checkpoint,
                    "snapshot_vbl": state_target,
                    "snapshot_actual_vbl": snapshot["actual_vbl"],
                    "target_vbl": target_vbl,
                    "replayed_vbl": replayed_vbl,
                    "stateload_command_s": load["command_s"],
                    "stateload_observable_s": load["observable_s"],
                    "restored_vbl": load["restored_vbl"],
                    "rewind_total_s": total_s,
                    "screenshot_match": sha256_file(shot) == reference["screenshot_sha256"],
                    "memory_match": sha256_file(mem) == reference["memory_sha256"],
                    "screenshot_sha256": sha256_file(shot),
                    "memory_sha256": sha256_file(mem),
                }
            )

        for checkpoint in sorted(checkpoints):
            state = work / f"state_{checkpoint - cadence:05d}.vbl"
            reference = result["forward_reference"][str(checkpoint)]
            target_vbl = int(reference["vbl"])
            runs = []
            for attempt in (1, 2):
                emu.stateload(state)
                emu.wait_vbl(target_vbl)
                shot = work / f"det_{checkpoint:05d}_{attempt}.png"
                emu.screenshot(shot)
                mem = work / f"det_{checkpoint:05d}_{attempt}.bin"
                emu.savebin(mem, mem_address, mem_size)
                runs.append(
                    {
                        "screenshot_sha256": sha256_file(shot),
                        "memory_sha256": sha256_file(mem),
                    }
                )
            result["replay_determinism"].append(
                {
                    "checkpoint_vbl": checkpoint,
                    "screenshot_match": runs[0]["screenshot_sha256"] == runs[1]["screenshot_sha256"],
                    "memory_match": runs[0]["memory_sha256"] == runs[1]["memory_sha256"],
                }
            )

    sizes = [item["size_bytes"] for item in result["snapshots"]]
    saves = [item["save_s"] for item in result["snapshots"]]
    observable_loads = [item["stateload_observable_s"] for item in result["rewinds"]]
    command_loads = [item["stateload_command_s"] for item in result["rewinds"]]
    median_size = sorted(sizes)[len(sizes) // 2]
    median_save = sorted(saves)[len(saves) // 2]
    median_observable_load = sorted(observable_loads)[len(observable_loads) // 2]
    median_command_load = sorted(command_loads)[len(command_loads) // 2]
    summary = {
        "snapshot_count": len(sizes),
        "size_bytes_min": min(sizes),
        "size_bytes_max": max(sizes),
        "size_bytes_median": median_size,
        "save_s_median": median_save,
        "save_s_max": max(saves),
        "stateload_s_median": median_observable_load,
        "stateload_observable_s_median": median_observable_load,
        "stateload_command_s_median": median_command_load,
        "disk_10s_bytes": median_size * (10 * 50 // cadence),
        "disk_60s_bytes": median_size * (60 * 50 // cadence),
        "disk_60s_mib": median_size * (60 * 50 // cadence) / (1024 * 1024),
        "save_plus_load_s": median_save + median_observable_load,
        "all_screenshot_matches": all(item["screenshot_match"] for item in result["rewinds"]),
        "all_memory_matches": all(item["memory_match"] for item in result["rewinds"]),
        "all_replay_deterministic": all(
            item["screenshot_match"] and item["memory_match"]
            for item in result["replay_determinism"]
        ),
        "all_rewinds_match": all(
            item["screenshot_match"] and item["memory_match"] for item in result["rewinds"]
        ),
        "rewind_total_s_max": max(item["rewind_total_s"] for item in result["rewinds"]),
        "cadence_estimates": cadence_estimates(median_size),
    }
    summary.update(
        rewind_verdict(summary, input_replay_evidence=input_replay_evidence)
    )
    summary["go_framebuffer"] = summary["go_static_checkpoints"]
    result["summary"] = summary
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--profile", default="st")
    parser.add_argument("--cadence", type=int, default=50)
    parser.add_argument("--last-vbl", type=int, default=500)
    parser.add_argument("--memory-address", type=lambda value: int(value, 0), default=MEM_ADDRESS)
    parser.add_argument("--memory-size", type=lambda value: int(value, 0), default=MEM_SIZE)
    parser.add_argument(
        "--input-replay-evidence",
        type=Path,
        default=None,
        help="JSON evidence record required for an input-replay GO",
    )
    parser.add_argument(
        "--input-replay-proven",
        action="store_true",
        help="deprecated bare flag; insufficient without --input-replay-evidence",
    )
    args = parser.parse_args(argv)
    out_dir = args.out or make_out_dir("time-travel")
    evidence = None
    if args.input_replay_evidence:
        evidence = json.loads(
            args.input_replay_evidence.read_text(encoding="utf-8")
        )
    elif args.input_replay_proven:
        print(
            "warning: --input-replay-proven without --input-replay-evidence is "
            "ignored; the verdict remains INCONCLUSIVE"
        )
    result = run(
        out_dir,
        profile=args.profile,
        cadence=args.cadence,
        last_vbl=args.last_vbl,
        mem_address=args.memory_address,
        mem_size=args.memory_size,
        input_replay_evidence=evidence,
    )
    write_json(out_dir / "time_travel.json", result)
    print(f"wrote {out_dir / 'time_travel.json'}")
    print(f"GO={result['summary']['go']} verdict={result['summary']['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
