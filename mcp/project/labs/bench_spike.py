"""P8.10 fixed-workload benchmark and regression-detection spike.

Three headless profiles (ST, STE, Falcon) are run ten times each. The spike
measures launch-to-control-socket, a fixed 500-VBL workload, debugger ``cpureg``
round-trip latency, screenshot latency, host CPU time, and peak RSS. A
deliberate host-level pause duty cycle injects an approximately ten-percent
slowdown; baseline and injected runs are interleaved in randomized order to
reduce ordering bias. The current shared host is explicitly not a dedicated
runner, so the result is harness feasibility rather than a benchmark GO.
"""

from __future__ import annotations

import argparse
import random
import time
from pathlib import Path
from typing import Any

from . import require_labs
from .harness import Hatari, make_out_dir, run_fixed_workload, write_json
from .stats import detect_regression, minimum_detectable_ratio, summary

require_labs()

REQUIRED_PROFILES = ("st", "ste", "falcon")
MIN_REPS = 10
MAX_WORKLOAD_COV = 0.05


def benchmark_design_ok(profiles: tuple[str, ...] | list[str], reps: int) -> bool:
    """Require the declared 3-profile x >=10-rep benchmark design."""
    return bool(
        len(profiles) == len(REQUIRED_PROFILES)
        and set(profiles) == set(REQUIRED_PROFILES)
        and reps >= MIN_REPS
    )


def gate_summary(
    *,
    profiles: tuple[str, ...] | list[str],
    reps: int,
    max_cov: float,
    detection: dict[str, Any],
    workload_returncodes: list[int | None],
    interactive_returncodes: list[int | None],
    dedicated_runner: bool,
) -> dict[str, Any]:
    design_ok = benchmark_design_ok(profiles, reps)
    workload_codes_ok = bool(workload_returncodes) and all(
        code == 0 for code in workload_returncodes
    )
    interactive_codes_ok = bool(interactive_returncodes) and all(
        code == 0 for code in interactive_returncodes
    )
    returncodes_ok = bool(workload_codes_ok and interactive_codes_ok)
    harness_ok = bool(
        design_ok
        and max_cov < MAX_WORKLOAD_COV
        and detection.get("detected")
        and returncodes_ok
    )
    go = bool(harness_ok and dedicated_runner)
    if not design_ok:
        verdict = (
            "INCONCLUSIVE: the gate requires exactly three profiles "
            "(st, ste, falcon) x at least 10 reps"
        )
    elif not returncodes_ok:
        verdict = (
            "INCONCLUSIVE: workload or interactive runs returned a nonzero/missing "
            "return code"
        )
    elif max_cov >= MAX_WORKLOAD_COV:
        verdict = (
            f"INCONCLUSIVE: workload CoV {max_cov:.1%} is not below "
            f"{MAX_WORKLOAD_COV:.0%}"
        )
    elif not detection.get("detected"):
        verdict = "INCONCLUSIVE: the injected regression was not detected"
    elif not dedicated_runner:
        verdict = (
            "INCONCLUSIVE (harness feasibility only): shared host; no benchmark "
            "regression GO"
        )
    else:
        verdict = (
            "GO: dedicated runner, 3 profiles x >=10 reps, zero return codes, "
            "variance and injected regression meet the threshold"
        )
    return {
        "profiles": list(profiles),
        "reps": reps,
        "design_ok": design_ok,
        "max_workload_cov": max_cov,
        "injected_ratio": detection.get("median_ratio"),
        "injected_detected": bool(detection.get("detected")),
        "workload_returncodes_ok": workload_codes_ok,
        "interactive_returncodes_ok": interactive_codes_ok,
        "interactive_returncodes": list(interactive_returncodes),
        "returncodes_ok": returncodes_ok,
        "interleaved_ab": True,
        "go_harness_feasibility": harness_ok,
        "go": go,
        "verdict": verdict,
    }


def interleaved_order(reps: int, *, seed: int = 8128) -> list[str]:
    """Return a reproducible, balanced randomized order for A/B runs."""
    rng = random.Random(seed)
    order: list[str] = []
    for _ in range(reps):
        pair = ["baseline", "injected"]
        rng.shuffle(pair)
        order.extend(pair)
    return order


def interactive_run(profile: str, out_dir: Path, *, reps: int = 10) -> dict[str, Any]:
    shots = out_dir / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    cpureg_ms: list[float] = []
    screenshot_ms: list[float] = []
    startup_s: list[float] = []
    peak_rss_kb = 0
    total_s = 0.0
    with Hatari(profile) as emu:
        start = time.perf_counter()
        emu.start()
        startup_s.append(emu.connect_s)
        time.sleep(0.25)
        for _ in range(10):
            t0 = time.perf_counter()
            emu.command("cpureg", timeout=5.0)
            cpureg_ms.append((time.perf_counter() - t0) * 1000.0)
        for index in range(5):
            shot = shots / f"{profile}_{index}.png"
            screenshot_ms.append(emu.screenshot(shot) * 1000.0)
        peak_rss_kb = emu.peak_rss_kb()
        total_s = time.perf_counter() - start
        emu.stop()
        returncode = emu.proc.returncode if emu.proc else None
    return {
        "profile": profile,
        "startup_s": startup_s[0],
        "total_s": total_s,
        "cpureg_ms": cpureg_ms,
        "screenshot_ms": screenshot_ms,
        "peak_rss_kb": peak_rss_kb,
        "returncode": returncode,
    }


def benchmark_profile(profile: str, out_dir: Path, *, reps: int) -> dict[str, Any]:
    workload = [run_fixed_workload(profile, run_vbls=500, benchmark=True) for _ in range(reps)]
    interactive = [interactive_run(profile, out_dir) for _ in range(reps)]
    returncodes = [run.returncode for run in workload]
    if any(code != 0 for code in returncodes):
        raise RuntimeError(f"{profile} fixed workload returned nonzero exit codes: {returncodes}")
    interactive_returncodes = [run.get("returncode") for run in interactive]
    workload_s = [run.total_s for run in workload]
    connect_s = [run.connect_s for run in workload]
    scenario_s = [max(0.0, run.total_s - run.connect_s) for run in workload]
    cpu_s = [run.cpu_s for run in workload]
    rss_mib = [run.peak_rss_kb / 1024.0 for run in workload]
    cpureg = [value for run in interactive for value in run["cpureg_ms"]]
    screenshot = [value for run in interactive for value in run["screenshot_ms"]]
    interactive_rss = [run["peak_rss_kb"] / 1024.0 for run in interactive]
    return {
        "profile": profile,
        "reps": reps,
        "returncodes": returncodes,
        "interactive_returncodes": interactive_returncodes,
        "workload_s": workload_s,
        "startup_connect_s": connect_s,
        "scenario_after_connect_s": scenario_s,
        "cpu_s": cpu_s,
        "peak_rss_mib": rss_mib,
        "interactive_peak_rss_mib": interactive_rss,
        "cpureg_ms": cpureg,
        "screenshot_ms": screenshot,
        "summary": {
            "workload_s": summary(workload_s),
            "startup_connect_s": summary(connect_s),
            "scenario_after_connect_s": summary(scenario_s),
            "cpu_s": summary(cpu_s),
            "peak_rss_mib": summary(rss_mib),
            "cpureg_ms": summary(cpureg),
            "screenshot_ms": summary(screenshot),
            "min_detectable_ratio_workload": minimum_detectable_ratio(workload_s, reps),
        },
    }


def run_injection(
    *, reps: int, seed: int = 8128, pause_ms: float = 6.5, period_ms: float = 65.0
) -> dict[str, Any]:
    order = interleaved_order(reps, seed=seed)
    baseline: list[float] = []
    injected: list[float] = []
    returncodes: list[int | None] = []
    for kind in order:
        if kind == "baseline":
            metrics = run_fixed_workload("st", run_vbls=500, benchmark=True)
            baseline.append(metrics.total_s)
        else:
            metrics = run_fixed_workload(
                "st", run_vbls=500, benchmark=True, pause_duty=(pause_ms, period_ms)
            )
            injected.append(metrics.total_s)
        returncodes.append(metrics.returncode)
    if any(code != 0 for code in returncodes):
        raise RuntimeError(f"injection runs returned nonzero exit codes: {returncodes}")
    return {
        "profile": "st",
        "method": f"SIGSTOP {pause_ms} ms every {period_ms} ms on the Hatari process",
        "order": order,
        "seed": seed,
        "baseline_s": baseline,
        "injected_s": injected,
        "returncodes": returncodes,
        "baseline_summary": summary(baseline),
        "injected_summary": summary(injected),
        "detection": detect_regression(baseline, injected, min_ratio=1.05),
    }


def run(
    out_dir: Path,
    *,
    reps: int = 10,
    profiles: tuple[str, ...] = ("st", "ste", "falcon"),
    dedicated_runner: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "spike": "P8.10",
        "run_vbls": 500,
        "reps": reps,
        "dedicated_runner": dedicated_runner,
        "profiles": {},
        "injection": {},
        "limitations": [
            "Shared development host, not a dedicated benchmark runner."
            if not dedicated_runner
            else "Declared dedicated runner; CPU isolation is still the operator's responsibility.",
            "The injected slowdown is a SIGSTOP duty cycle, not an emulator option.",
            "Absolute numbers are host-specific and must not be compared across hosts.",
        ],
    }
    for profile in profiles:
        result["profiles"][profile] = benchmark_profile(profile, out_dir, reps=reps)

    result["injection"] = run_injection(reps=reps)
    covs = [
        float(result["profiles"][name]["summary"]["workload_s"]["cov"]) for name in profiles
    ]
    max_cov = max(covs)
    detection = result["injection"]["detection"]
    workload_returncodes = [
        code for item in result["profiles"].values() for code in item["returncodes"]
    ] + list(result["injection"]["returncodes"])
    interactive_returncodes = [
        code
        for item in result["profiles"].values()
        for code in item.get("interactive_returncodes", [])
    ]
    result["summary"] = gate_summary(
        profiles=profiles,
        reps=reps,
        max_cov=max_cov,
        detection=detection,
        workload_returncodes=workload_returncodes,
        interactive_returncodes=interactive_returncodes,
        dedicated_runner=dedicated_runner,
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--reps", type=int, default=10)
    parser.add_argument(
        "--profiles", nargs="+", default=["st", "ste", "falcon"], choices=["st", "ste", "falcon"]
    )
    parser.add_argument("--dedicated-runner", action="store_true")
    args = parser.parse_args(argv)
    out_dir = args.out or make_out_dir("bench")
    result = run(
        out_dir,
        reps=args.reps,
        profiles=tuple(args.profiles),
        dedicated_runner=args.dedicated_runner,
    )
    write_json(out_dir / "bench.json", result)
    print(f"wrote {out_dir / 'bench.json'}")
    for name, item in result["profiles"].items():
        work = item["summary"]["workload_s"]
        cpu = item["summary"]["cpu_s"]
        print(
            f"{name}: median={work['median']:.3f}s cov={work['cov']:.1%} "
            f"cpu={cpu['median']:.3f}s boot={item['summary']['startup_connect_s']['median']:.3f}s "
            f"rss={item['summary']['peak_rss_mib']['median']:.1f}MiB "
            f"cpureg={item['summary']['cpureg_ms']['median']:.2f}ms "
            f"shot={item['summary']['screenshot_ms']['median']:.2f}ms"
        )
    inj = result["injection"]
    print(
        f"injection: ratio={inj['detection']['median_ratio']:.3f} "
        f"p={inj['detection']['p_value']:.5f} detected={inj['detection']['detected']}"
    )
    print(f"GO={result['summary']['go']} verdict={result['summary']['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
