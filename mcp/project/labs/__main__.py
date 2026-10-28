"""Command-line entry point for the non-shipping P8 lab spikes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import require_labs
from . import bench_spike, coverage_gap_spike, dwarf_spike, time_travel_spike
from .harness import make_out_dir, write_json

require_labs()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "spike",
        nargs="?",
        default="all",
        choices=["bench", "time-travel", "dwarf", "coverage", "all"],
    )
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--reps", type=int, default=10)
    parser.add_argument("--dedicated-runner", action="store_true")
    parser.add_argument("--trace-runs", type=int, default=3)
    parser.add_argument("--from-json", type=Path, default=None)
    parser.add_argument("--manual-review-done", action="store_true")
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
    parser.add_argument("--memory-address", type=lambda value: int(value, 0), default=time_travel_spike.MEM_ADDRESS)
    parser.add_argument("--memory-size", type=lambda value: int(value, 0), default=time_travel_spike.MEM_SIZE)
    args = parser.parse_args(argv)
    out_dir = args.out or make_out_dir("all")
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs: dict[str, str] = {}

    if args.spike in {"bench", "all"}:
        result = bench_spike.run(
            out_dir / "bench", reps=args.reps, dedicated_runner=args.dedicated_runner
        )
        write_json(out_dir / "bench" / "bench.json", result)
        outputs["bench"] = str(out_dir / "bench" / "bench.json")
    if args.spike in {"time-travel", "all"}:
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
        result = time_travel_spike.run(
            out_dir / "time-travel",
            mem_address=args.memory_address,
            mem_size=args.memory_size,
            input_replay_evidence=evidence,
        )
        write_json(out_dir / "time-travel" / "time_travel.json", result)
        outputs["time-travel"] = str(out_dir / "time-travel" / "time_travel.json")
    if args.spike in {"dwarf", "all"}:
        result = dwarf_spike.run(out_dir / "dwarf")
        write_json(out_dir / "dwarf" / "dwarf.json", result)
        outputs["dwarf"] = str(out_dir / "dwarf" / "dwarf.json")
    if args.spike in {"coverage", "all"}:
        manual_review = {"done": bool(args.manual_review_done), "notes": ""}
        if args.from_json:
            data = json.loads(args.from_json.read_text(encoding="utf-8"))
            result = dict(data)
            result["summary"] = coverage_gap_spike.build_summary(
                data, manual_review=manual_review
            )
        else:
            result = coverage_gap_spike.run(
                out_dir / "coverage",
                trace_runs=args.trace_runs,
                manual_review=manual_review,
            )
        write_json(out_dir / "coverage.json", result)
        outputs["coverage"] = str(out_dir / "coverage.json")

    print(f"out_dir={out_dir}")
    for name, path in outputs.items():
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
