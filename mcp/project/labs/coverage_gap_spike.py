"""P8.3 raw stock-profiler coverage spike on a known AHCC binary.

This is deliberately *not* a P6.11 integration test. It compares two stock
Hatari mechanisms:

* the CPU profiler, enabled inside a debugger session and saved as text; and
* full ``--trace cpu_disasm`` output for a fixed workload.

Both are mapped to the AHCC ``HELLO_ST.MAP`` symbol offsets. The spike reports
function-level execution data, zero-sample functions, volume, overhead, and
stability. It does not provide line/branch coverage and does not prove that the
P6.11 backend can expose the same data.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import statistics
import time
from collections import Counter
from pathlib import Path
from typing import Any

from . import require_labs
from .harness import Hatari, make_out_dir, run_fixed_workload, write_json

require_labs()

DEFAULT_BASE = 0xF176
EXIT_OFFSET = 454
RUN_VBLS = 600
MIN_ACTIVE_FUNCTIONS = 5
MIN_SAMPLE_ROWS = 5
MIN_SAMPLE_AGREEMENT = 0.90
MAX_PROFILE_OVERHEAD_RATIO = 2.0
P6_11_SUPPORTED_STATUSES = {"supported", "pass", "passed", "ok"}
MAP_RE = re.compile(r"^\s*(\d+)\s+(\d+)\[\s*(\d+)\]\s+TEXT\s+(.*)$")
PROFILE_HEADER_RE = re.compile(r"^PROGRAM_TEXT:\s*0x([0-9a-fA-F]+)-0x([0-9a-fA-F]+)")
PROFILE_ROW_RE = re.compile(r"^([0-9a-fA-F]{8})\s+.*\((\d+),")
TRACE_ROW_RE = re.compile(r"^([0-9a-fA-F]{8})\s+")


def load_map(path: Path) -> list[dict[str, Any]]:
    entries: dict[int, dict[str, Any]] = {}
    for line in path.read_text(encoding="latin-1").splitlines():
        match = MAP_RE.match(line)
        if not match:
            continue
        offset = int(match.group(1))
        size = int(match.group(3))
        name = match.group(4).strip()
        if not name or size <= 0:
            continue
        previous = entries.get(offset)
        if previous is None or (previous["name"].startswith("<") and not name.startswith("<")):
            entries[offset] = {"offset": offset, "size": size, "name": name}
    return sorted(entries.values(), key=lambda item: item["offset"])


def assign_functions(offsets: list[int], functions: list[dict[str, Any]]) -> dict[str, int]:
    starts = [(item["offset"], item["offset"] + item["size"], item["name"]) for item in functions]
    starts.sort()
    counts: Counter[str] = Counter()
    for offset in offsets:
        found = None
        for start, end, name in starts:
            if start <= offset < end:
                found = name
            elif start > offset:
                break
        if found:
            counts[found] += 1
    return dict(counts)


def parse_profile(path: Path) -> dict[str, Any]:
    counts: Counter[int] = Counter()
    base = None
    end = None
    for line in path.read_text(encoding="latin-1", errors="replace").splitlines():
        header = PROFILE_HEADER_RE.match(line)
        if header:
            base = int(header.group(1), 16)
            end = int(header.group(2), 16)
            continue
        match = PROFILE_ROW_RE.match(line)
        if match:
            counts[int(match.group(1), 16)] += int(match.group(2))
    if base is None:
        base = DEFAULT_BASE
    offsets = [address - base for address in counts if base <= address < (end or 0x200000)]
    return {
        "base": base,
        "end": end,
        "unique_pcs": len(counts),
        "instructions": sum(counts.values()),
        "offsets": offsets,
        "profile_counts": dict(counts),
    }


def parse_trace(path: Path, *, base: int | None = None) -> dict[str, Any]:
    counts: Counter[int] = Counter()
    lines = 0
    for line in path.read_text(encoding="latin-1", errors="replace").splitlines():
        match = TRACE_ROW_RE.match(line)
        if match:
            counts[int(match.group(1), 16)] += 1
            lines += 1
    inferred = False
    if base is None:
        low = [pc for pc in counts if 0x1000 <= pc < 0x20000]
        base = min(low) if low else None
        inferred = True
    offsets = (
        [pc - base for pc in counts if base is not None and base <= pc < base + 0x4000]
        if base is not None
        else []
    )
    return {
        "lines": lines,
        "unique_pcs": len(counts),
        "base": base,
        "base_source": "inferred" if inferred else "explicit",
        "offsets": offsets,
        "trace_counts": dict(counts),
    }


def _meaningful(functions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in functions if not item["name"].startswith("<")]


def _run_profile_once(
    hd: Path, out_path: Path, *, base: int, exit_addr: int
) -> dict[str, Any]:
    with Hatari("ste") as emu:
        emu.start(harddrive=hd, autostart="C:\\HELLO_ST.TOS")
        emu.command(f"breakpoint pc = ${base:x} :once", timeout=5.0)
        emu.wait_for_line("condition(s) matched", timeout=15.0)
        cpu_start = emu.guest_cpu_time_s()
        start = time.perf_counter()
        emu.send_stdin("profile on")
        emu.wait_for_line("Profiling enabled.", timeout=5.0)
        emu.send_stdin(f"b pc = ${exit_addr:x} :once")
        emu.send_stdin("cont")
        emu.wait_for_line("condition(s) matched", timeout=15.0)
        elapsed = time.perf_counter() - start
        cpu_end = emu.guest_cpu_time_s()
        emu.send_stdin(f'profile save "{out_path}"')
        emu.wait_for_line("Disassembled", timeout=10.0)
        emu.send_stdin("profile off")
        emu.send_stdin("quit")
        try:
            emu.wait(timeout=10.0)
        except Exception:
            pass
    return {
        "elapsed_s": elapsed,
        "cpu_s": max(0.0, cpu_end - cpu_start),
        "profile_bytes": out_path.stat().st_size,
    }


def _run_baseline_once(hd: Path, *, base: int, exit_addr: int) -> dict[str, Any]:
    with Hatari("ste") as emu:
        emu.start(harddrive=hd, autostart="C:\\HELLO_ST.TOS")
        emu.command(f"breakpoint pc = ${base:x} :once", timeout=5.0)
        emu.wait_for_line("condition(s) matched", timeout=15.0)
        cpu_start = emu.guest_cpu_time_s()
        start = time.perf_counter()
        emu.send_stdin(f"b pc = ${exit_addr:x} :once")
        emu.send_stdin("cont")
        emu.wait_for_line("condition(s) matched", timeout=15.0)
        elapsed = time.perf_counter() - start
        cpu_end = emu.guest_cpu_time_s()
        emu.send_stdin("quit")
        try:
            emu.wait(timeout=10.0)
        except Exception:
            pass
    return {"elapsed_s": elapsed, "cpu_s": max(0.0, cpu_end - cpu_start)}


def _prepare_workspace(out_dir: Path, binary: Path) -> Path:
    hd = out_dir / "hd"
    hd.mkdir(parents=True, exist_ok=True)
    shutil.copy2(binary, hd / binary.name)
    return hd


def _signature(run: dict[str, Any]) -> tuple[tuple[str, int], ...]:
    counts = run.get("function_counts") or {}
    return tuple(sorted((name, int(value)) for name, value in counts.items() if int(value) > 0))


def _run_has_data(run: dict[str, Any]) -> bool:
    unique = int(run.get("unique_pcs") or 0)
    instructions = int(run.get("instructions") or run.get("trace_lines") or 0)
    mapped = sum(int(value) for value in (run.get("function_counts") or {}).values())
    return unique > 0 and instructions > 0 and mapped > 0


def _combined_counts(runs: list[dict[str, Any]]) -> Counter[str]:
    combined: Counter[str] = Counter()
    for run in runs:
        for name, value in (run.get("function_counts") or {}).items():
            combined[name] += int(value)
    return combined


def evaluate_p6_11_integration(data: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Validate explicit P6.11 backend evidence.

    A boolean, a missing key, or a raw stock-profiler run is not enough. The
    evidence record must identify the backend, scenario, artifact, run ids and
    positive sample source so a function-level GO cannot be produced by
    fabricated positive counts alone.
    """
    evidence = data.get("p6_11_integration")
    if not isinstance(evidence, dict):
        return False, {
            "present": False,
            "reason": "missing P6.11 integration evidence record",
        }
    status = str(evidence.get("status", "")).lower()
    samples = evidence.get("sample_count")
    if samples is None:
        samples = evidence.get("samples")
    checks = {
        "status_supported": status in P6_11_SUPPORTED_STATUSES,
        "backend_named": bool(evidence.get("backend")),
        "scenario_identified": bool(evidence.get("scenario_id") or evidence.get("scenario")),
        "artifact_identified": bool(
            evidence.get("artifact") or evidence.get("artifact_path")
        ),
        "profile_run_ids": bool(evidence.get("profile_run_ids")),
        "trace_run_ids": bool(evidence.get("trace_run_ids")),
        "positive_samples": bool(
            evidence.get("samples_positive") is True
            or (isinstance(samples, int) and samples > 0)
            or (isinstance(samples, list) and len(samples) > 0)
        ),
    }
    return all(checks.values()), {
        "present": True,
        "status": status or None,
        "checks": checks,
    }


def _gap_entries(raw: Any) -> list[dict[str, Any]] | None:
    if isinstance(raw, dict):
        if isinstance(raw.get("gaps"), list):
            raw = raw["gaps"]
        else:
            entries = []
            for function, value in raw.items():
                if isinstance(value, dict):
                    entries.append({"function": function, **value})
                else:
                    entries.append({"function": function, "scenario_step": value})
            raw = entries
    if not isinstance(raw, list):
        return None
    entries = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        function = item.get("function")
        if isinstance(function, list):
            functions = [str(name) for name in function if str(name)]
        else:
            functions = [str(function)] if function else []
        step = item.get("scenario_step")
        if step is None:
            step = item.get("step")
        if step is None:
            step = item.get("checkpoint")
        for name in functions:
            entries.append(
                {
                    "function": name,
                    "scenario_step": step,
                    "artifact_checkpoint": item.get("artifact_checkpoint"),
                }
            )
    return entries


def evaluate_scenario_step_gap_map(
    data: dict[str, Any], zero_sample_functions: list[str]
) -> tuple[bool, dict[str, Any]]:
    """Require every zero-sample gap to be tied to a scenario step."""
    raw = data.get("scenario_step_gap_map")
    if raw is None:
        evidence = data.get("p6_11_integration")
        if isinstance(evidence, dict):
            raw = evidence.get("scenario_step_gap_map")
    entries = _gap_entries(raw)
    if not entries:
        return False, {
            "present": False,
            "reason": "missing scenario-step gap mapping",
        }
    valid = [
        item
        for item in entries
        if item["scenario_step"] not in (None, "") and item["function"]
    ]
    mapped = sorted({item["function"] for item in valid})
    missing = sorted(set(zero_sample_functions) - set(mapped))
    ok = bool(valid) and not missing
    return ok, {
        "present": True,
        "entries": len(valid),
        "mapped_functions": mapped,
        "missing_zero_functions": missing,
    }


def build_summary(
    data: dict[str, Any],
    *,
    sample_size: int = 10,
    manual_review: dict[str, Any] | None = None,
    min_active_functions: int = MIN_ACTIVE_FUNCTIONS,
) -> dict[str, Any]:
    profile_runs = list(data.get("profile_runs") or [])
    trace_runs = list(data.get("trace_runs") or [])
    profile_valid = bool(profile_runs) and all(_run_has_data(run) for run in profile_runs)
    trace_valid = bool(trace_runs) and all(_run_has_data(run) for run in trace_runs)
    profile_signatures = {_signature(run) for run in profile_runs}
    trace_signatures = {_signature(run) for run in trace_runs}
    profile_stable = bool(profile_valid and len(profile_signatures) == 1)
    trace_stable = bool(trace_valid and len(trace_signatures) == 1)

    profile_counts = _combined_counts(profile_runs)
    trace_counts = _combined_counts(trace_runs)
    zero_profile = sorted(
        set(data.get("zero_sample_functions") or [])
        | set((data.get("summary") or {}).get("zero_sample_functions") or [])
    )
    active_profile = sorted(profile_counts)
    active_trace = sorted(trace_counts)
    active_union = sorted(set(active_profile) | set(active_trace))
    active_count = len(active_union)

    candidates = set(profile_counts) | set(trace_counts) | set(zero_profile)
    ordered = sorted(
        candidates,
        key=lambda name: (-(profile_counts.get(name, 0) + trace_counts.get(name, 0)), name),
    )
    sample_names = ordered[:sample_size]
    sample_rows = [
        {
            "function": name,
            "profile": profile_counts.get(name, 0),
            "trace": trace_counts.get(name, 0),
            "agree": profile_counts.get(name, 0) == trace_counts.get(name, 0),
            "zero_zero": profile_counts.get(name, 0) == 0 and trace_counts.get(name, 0) == 0,
        }
        for name in sample_names
    ]
    active_rows = [row for row in sample_rows if not row["zero_zero"]]
    sample_agreement = (
        sum(1 for row in active_rows if row["agree"]) / len(active_rows) if active_rows else 0.0
    )

    profile_cpu = [float(run["cpu_s"]) for run in profile_runs if run.get("cpu_s")]
    baseline_cpu = [float(run["cpu_s"]) for run in data.get("baseline_runs", []) if run.get("cpu_s")]
    if profile_cpu and baseline_cpu and statistics.median(baseline_cpu) > 0:
        cpu_overhead: float | None = statistics.median(profile_cpu) / statistics.median(baseline_cpu)
        if len(profile_cpu) >= 3 and len(baseline_cpu) >= 3:
            overhead_status = "measured_host_cpu_entry_exit"
        else:
            overhead_status = (
                "INCONCLUSIVE: fewer than 3 entry->exit CPU samples per side "
                f"(profile={len(profile_cpu)}, baseline={len(baseline_cpu)})"
            )
    else:
        cpu_overhead = None
        overhead_status = "INCONCLUSIVE: no entry->exit CPU samples in dataset"
    overhead_measured = bool(
        overhead_status == "measured_host_cpu_entry_exit"
        and cpu_overhead is not None
        and cpu_overhead > 0
        and len(profile_cpu) >= 3
        and len(baseline_cpu) >= 3
    )
    overhead_ok = bool(
        overhead_measured and cpu_overhead is not None and cpu_overhead <= MAX_PROFILE_OVERHEAD_RATIO
    )

    profile_elapsed = [float(run["elapsed_s"]) for run in profile_runs if "elapsed_s" in run]
    baseline_elapsed = [float(run["elapsed_s"]) for run in data.get("baseline_runs", []) if "elapsed_s" in run]
    wall_overhead = (
        statistics.median(profile_elapsed) / statistics.median(baseline_elapsed)
        if profile_elapsed and baseline_elapsed and statistics.median(baseline_elapsed) > 0
        else None
    )

    data_ok = bool(profile_valid and trace_valid)
    active_ok = active_count >= min_active_functions
    sample_ok = len(active_rows) >= min(MIN_SAMPLE_ROWS, max(1, min(sample_size, active_count))) and sample_agreement >= MIN_SAMPLE_AGREEMENT
    manual_review_done = bool(manual_review and manual_review.get("done"))
    p6_11_ok, p6_11_detail = evaluate_p6_11_integration(data)
    gap_map_ok, gap_map_detail = evaluate_scenario_step_gap_map(data, zero_profile)
    go_function_level = bool(
        data_ok
        and profile_stable
        and trace_stable
        and active_ok
        and sample_ok
        and manual_review_done
        and p6_11_ok
        and overhead_ok
        and gap_map_ok
    )

    if not profile_runs or not trace_runs:
        verdict = "NO-GO: profile or trace runs are missing"
    elif not data_ok:
        verdict = "NO-GO: profile/trace contained no positive PCs, instructions, or mapped functions"
    elif not (profile_stable and trace_stable):
        verdict = "NO-GO: active function set or hit counts were not stable across runs"
    elif not active_ok:
        verdict = f"NO-GO: only {active_count} mapped active functions (minimum {min_active_functions})"
    elif not sample_ok:
        verdict = "NO-GO: hit-count agreement on active functions is below the threshold"
    else:
        blockers = []
        if not manual_review_done:
            blockers.append("manual disassembly review was not performed")
        if not p6_11_ok:
            evidence = data.get("p6_11_integration")
            if isinstance(evidence, dict) and str(evidence.get("status", "")).lower() in {
                "unsupported",
                "no-go",
                "no_go",
            }:
                blockers.append("P6.11 backend reports unsupported/NO-GO")
            else:
                blockers.append("P6.11 backend integration evidence is missing or invalid")
        if not overhead_ok:
            blockers.append(
                f"profiler overhead is not a measured entry->exit result within "
                f"{MAX_PROFILE_OVERHEAD_RATIO:g}x ({overhead_status})"
            )
        if not gap_map_ok:
            blockers.append("zero-sample gaps are not mapped to scenario steps")
        if blockers:
            prefix = "NO-GO" if any(
                "unsupported/NO-GO" in blocker for blocker in blockers
            ) else "INCONCLUSIVE"
            verdict = f"{prefix}: " + "; ".join(blockers)
        else:
            verdict = (
                "GO (limited): function-level sampling with P6.11 evidence, measured "
                "entry->exit overhead, and scenario-step gap mapping; line/branch "
                "coverage remains NO-GO"
            )

    return {
        "profile_base": profile_runs[0].get("base") if profile_runs else None,
        "profile_end": profile_runs[0].get("end") if profile_runs else None,
        "profile_instructions": int(profile_runs[0].get("instructions") or 0)
        if profile_runs
        else 0,
        "profile_unique_pcs": int(statistics.median(r.get("unique_pcs", 0) for r in profile_runs))
        if profile_runs
        else 0,
        "profile_bytes": profile_runs[0].get("profile_bytes") if profile_runs else None,
        "profile_active_functions": active_count,
        "active_functions": active_union,
        "zero_sample_functions": zero_profile,
        "profile_stable": profile_stable,
        "trace_stable": trace_stable,
        "profile_valid": profile_valid,
        "trace_valid": trace_valid,
        "trace_base": trace_runs[0].get("base") if trace_runs else None,
        "trace_base_source": trace_runs[0].get("base_source") if trace_runs else None,
        "trace_bytes_median": statistics.median(
            float(item.get("trace_bytes") or 0) for item in trace_runs
        )
        if trace_runs
        else 0,
        "trace_lines_median": statistics.median(
            float(item.get("trace_lines") or 0) for item in trace_runs
        )
        if trace_runs
        else 0,
        "profile_overhead_ratio": wall_overhead,
        "profile_overhead_ratio_boot_dominated": wall_overhead,
        "profile_cpu_overhead_ratio": cpu_overhead,
        "profile_cpu_samples": len(profile_cpu),
        "baseline_cpu_samples": len(baseline_cpu),
        "overhead_status": overhead_status,
        "overhead_measured": overhead_measured,
        "overhead_ok": overhead_ok,
        "overhead_budget_ratio": MAX_PROFILE_OVERHEAD_RATIO,
        "sample": sample_rows,
        "sample_active_rows": len(active_rows),
        "sample_agreement": sample_agreement,
        "manual_review": manual_review or {"done": False},
        "manual_review_done": manual_review_done,
        "p6_11_integration": p6_11_ok,
        "p6_11_integration_status": (
            "supported: explicit evidence record validated"
            if p6_11_ok
            else "missing/invalid; raw stock profiler only"
        ),
        "p6_11_integration_detail": p6_11_detail,
        "scenario_step_gap_mapping_ok": gap_map_ok,
        "scenario_step_gap_mapping": gap_map_detail,
        "go_function_level": go_function_level,
        "go_function_level_data_only": bool(
            data_ok and profile_stable and trace_stable and active_ok and sample_ok
        ),
        "go_trace_level": False,
        "go_line_level": False,
        "verdict": verdict,
    }


def run(
    out_dir: Path,
    *,
    base: int = DEFAULT_BASE,
    exit_offset: int = EXIT_OFFSET,
    run_vbls: int = RUN_VBLS,
    trace_runs: int = 3,
    sample_size: int = 10,
    map_path: Path | None = None,
    binary: Path | None = None,
    manual_review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    binary = binary or (
        Path(__file__).resolve().parent.parent / "tools/ahcc/ahcc/HELLO/HELLO_ST.TOS"
    )
    map_path = map_path or binary.with_suffix(".MAP")
    hd = _prepare_workspace(out_dir, binary)
    functions = load_map(map_path)
    meaningful = _meaningful(functions)
    exit_address = base + exit_offset
    result: dict[str, Any] = {
        "spike": "P8.3",
        "binary": str(binary),
        "map": str(map_path),
        "run_vbls": run_vbls,
        "program_base": base,
        "exit_address": exit_address,
        "map_functions": len(functions),
        "meaningful_functions": len(meaningful),
        "profile_runs": [],
        "baseline_runs": [],
        "trace_runs": [],
        "fixed_baseline_runs": [],
        "limitations": [
            "Raw stock Hatari profiler only; P6.11 backend integration is untested.",
            "Hatari symbols carry addresses, not source lines.",
            "No DWARF is present in the AHCC TOS/ADB/PDB outputs.",
            "The profiler must be enabled inside a debugger session; control commands reset its buffers.",
            "Function ranges come from the AHCC map offsets plus the configured load base.",
            "The profile window is program entry->exit; the trace window is a fixed VBL count including boot.",
            "Manual disassembly review of the sample was not performed.",
        ],
    }

    profile_path = out_dir / "profile.txt"
    for _ in range(3):
        run_info = _run_profile_once(hd, profile_path, base=base, exit_addr=exit_address)
        parsed = parse_profile(profile_path)
        by_func = assign_functions(parsed["offsets"], functions)
        active = sorted(name for name, count in by_func.items() if count > 0)
        run_info.update(
            {
                "base": parsed["base"],
                "end": parsed["end"],
                "unique_pcs": parsed["unique_pcs"],
                "instructions": parsed["instructions"],
                "active_functions": active,
                "function_counts": by_func,
            }
        )
        result["profile_runs"].append(run_info)

    for _ in range(3):
        result["baseline_runs"].append(_run_baseline_once(hd, base=base, exit_addr=exit_address))

    for _ in range(3):
        metrics = run_fixed_workload(
            "ste",
            run_vbls=run_vbls,
            benchmark=True,
            harddrive=hd,
            autostart="C:\\HELLO_ST.TOS",
        )
        result["fixed_baseline_runs"].append(
            {"total_s": metrics.total_s, "cpu_s": metrics.cpu_s, "returncode": metrics.returncode}
        )

    traces = out_dir / "traces"
    traces.mkdir(parents=True, exist_ok=True)
    for index in range(trace_runs):
        trace_path = traces / f"trace_{index}.txt"
        metrics = run_fixed_workload(
            "ste",
            run_vbls=run_vbls,
            benchmark=True,
            trace_flags="cpu_disasm",
            trace_file=trace_path,
            harddrive=hd,
            autostart="C:\\HELLO_ST.TOS",
        )
        size = trace_path.stat().st_size
        parsed = parse_trace(trace_path, base=base)
        by_func = assign_functions(parsed["offsets"], functions)
        active = sorted(name for name, count in by_func.items() if count > 0)
        result["trace_runs"].append(
            {
                "trace_bytes": size,
                "trace_lines": parsed["lines"],
                "unique_pcs": parsed["unique_pcs"],
                "base": parsed["base"],
                "base_source": parsed["base_source"],
                "total_s": metrics.total_s,
                "cpu_s": metrics.cpu_s,
                "returncode": metrics.returncode,
                "active_functions": active,
                "function_counts": by_func,
            }
        )
        trace_path.unlink(missing_ok=True)

    profile_counts = _combined_counts(result["profile_runs"])
    result["zero_sample_functions"] = sorted(
        item["name"] for item in meaningful if profile_counts.get(item["name"], 0) == 0
    )
    result["summary"] = build_summary(
        result, sample_size=sample_size, manual_review=manual_review
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--from-json", type=Path, default=None, help="audit retained JSON without running Hatari")
    parser.add_argument("--base", type=lambda value: int(value, 0), default=DEFAULT_BASE)
    parser.add_argument("--exit-offset", type=int, default=EXIT_OFFSET)
    parser.add_argument("--run-vbls", type=int, default=RUN_VBLS)
    parser.add_argument("--trace-runs", type=int, default=3)
    parser.add_argument("--sample-size", type=int, default=10)
    parser.add_argument("--map", type=Path, default=None)
    parser.add_argument("--binary", type=Path, default=None)
    parser.add_argument("--manual-review-done", action="store_true")
    parser.add_argument("--manual-review-notes", default="")
    args = parser.parse_args(argv)
    out_dir = args.out or make_out_dir("coverage")
    manual_review = {
        "done": bool(args.manual_review_done),
        "notes": args.manual_review_notes,
    }
    if args.from_json:
        data = json.loads(args.from_json.read_text(encoding="utf-8"))
        result = dict(data)
        result["summary"] = build_summary(
            data, sample_size=args.sample_size, manual_review=manual_review
        )
    else:
        result = run(
            out_dir,
            base=args.base,
            exit_offset=args.exit_offset,
            run_vbls=args.run_vbls,
            trace_runs=args.trace_runs,
            sample_size=args.sample_size,
            map_path=args.map,
            binary=args.binary,
            manual_review=manual_review,
        )
    write_json(out_dir / "coverage.json", result)
    print(f"wrote {out_dir / 'coverage.json'}")
    summary = result["summary"]
    print(
        f"profile active={summary['profile_active_functions']} "
        f"stable={summary['profile_stable']} data={summary['profile_valid']}/{summary['trace_valid']} "
        f"cpu_overhead={summary['profile_cpu_overhead_ratio']} ({summary['overhead_status']})"
    )
    print(
        f"trace bytes={summary['trace_bytes_median']:.0f} "
        f"MiB={summary['trace_bytes_median'] / (1024 * 1024):.1f} "
        f"stable={summary['trace_stable']}"
    )
    print(
        f"sample active={summary['sample_active_rows']} agreement={summary['sample_agreement']:.2f} "
        f"manual_review={summary['manual_review_done']} zero={len(summary['zero_sample_functions'])}"
    )
    print(f"GO function={summary['go_function_level']} verdict={summary['verdict']}")
    return 0 if summary["go_function_level"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
