"""Command line entry point for scenario validation, running and corpus listing.

Run as ``python -m hatari_mcp.scenario_cli``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

from .corpus import discover_corpus
from .errors import HatariError
from .services.scenario import (
    EXIT_FAILED,
    EXIT_INFRA,
    EXIT_INTERNAL,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_USAGE,
    ScenarioEngine,
    ScenarioError,
    measure_repeatability,
    replay_bundle,
)


def _print_error(exc: BaseException, *, as_json: bool) -> None:
    if isinstance(exc, HatariError):
        payload = exc.as_dict()
    else:
        payload = {"ok": False, "error": type(exc).__name__, "message": str(exc)}
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"error: {payload.get('error')}: {payload.get('message')}", file=sys.stderr)
        if payload.get("hint"):
            print(f"hint: {payload['hint']}", file=sys.stderr)


def _engine_from_args(args: argparse.Namespace) -> ScenarioEngine:
    return ScenarioEngine(
        server_python=args.server_python,
        server_cmd=args.server_cmd,
        max_cases=args.max_cases,
        fail_fast=args.fail_fast,
        jobs=args.jobs,
        fail_on_skip=args.fail_on_skip,
        record=getattr(args, "record", False),
    )


def _classify_exit(report: dict[str, Any], exit_code: int) -> int:
    if exit_code == EXIT_OK:
        return EXIT_OK
    for case in report.get("cases", []):
        failure = case.get("failure") or {}
        text = f"{failure.get('error')} {failure.get('message')}".lower()
        if any(token in text for token in ("missing_hatari", "missing_tos", "startup_failed")):
            return EXIT_INFRA
        if failure.get("error") == "internal_error":
            return EXIT_INTERNAL
    return exit_code


def cmd_validate(args: argparse.Namespace) -> int:
    engine = _engine_from_args(args)
    result = engine.validate(args.file)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"ok: {result['scenario']} ({result['cases']} case(s), scenario v{result['version']})")
        for case in result["resolved_cases"]:
            axes = ",".join(f"{key}={value}" for key, value in sorted(case["axes"].items()))
            print(f"  {case['case']} {axes} {case['hash'][:12]}")
        for warning in result.get("warnings", []):
            print(f"warning: {warning}", file=sys.stderr)
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    engine = _engine_from_args(args)
    artifacts = Path(args.artifacts).expanduser() if args.artifacts else Path(tempfile.gettempdir()) / "hatari-mcp-artifacts"
    result = asyncio.run(engine.run(args.file, artifacts_root=artifacts))
    if args.junit:
        target = Path(args.junit).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(result.artifacts / "junit.xml", target)
    if args.json:
        print(json.dumps(result.report, indent=2, sort_keys=True))
    else:
        summary = result.report["summary"]
        print(
            f"run {result.run_id}: "
            + ", ".join(f"{key}={value}" for key, value in summary.items())
        )
        for case in result.cases:
            failure = case.get("failure") or {}
            detail = f" ({failure.get('error')}: {failure.get('step_id') or failure.get('message')})" if failure else ""
            print(f"  {case['status']:9} {case['case']}{detail}")
        print(f"artifacts: {result.artifacts}")
    return _classify_exit(result.report, result.exit_code)


def cmd_list_corpus(args: argparse.Namespace) -> int:
    data = discover_corpus()
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return EXIT_OK
    for name, project in data["projects"].items():
        status = "ok" if project["available"] else "skipped"
        print(f"{name}: {status} root={project['root']} env={project['env_var']}")
        if project.get("skip_reason"):
            print(f"  reason: {project['skip_reason']}")
        if project.get("prebuilt"):
            print(f"  prebuilt: {len(project['prebuilt'])}")
        for sub_name, sub in project.get("subprojects", {}).items():
            state = "ok" if sub.get("available") else "skipped"
            print(f"  {sub_name}: {state}")
    return EXIT_OK


def cmd_repeat(args: argparse.Namespace) -> int:
    artifacts = Path(args.artifacts).expanduser() if args.artifacts else Path(tempfile.gettempdir()) / "hatari-mcp-repeat"
    per_case: dict[str, list[dict[str, Any]]] = {}
    runs: list[dict[str, Any]] = []
    for index in range(args.repeats):
        engine = _engine_from_args(args)
        engine.run_id = f"repeat-{index}-{engine.run_id}"
        result = asyncio.run(engine.run(args.file, artifacts_root=artifacts))
        runs.append({"run_id": result.run_id, "summary": result.report["summary"], "artifacts": str(result.artifacts)})
        for case in result.cases:
            per_case.setdefault(case["case"], []).append(case.get("observables") or {})
    cases = {
        case_id: measure_repeatability(runs=observables)
        for case_id, observables in sorted(per_case.items())
    }
    runs_passed = all(
        run["summary"].get("failed", 0) == 0
        and run["summary"].get("error", 0) == 0
        and run["summary"].get("cancelled", 0) == 0
        for run in runs
    )
    report = {
        "ok": bool(cases) and runs_passed and all(item["repeatable"] for item in cases.values()),
        "repeats": args.repeats,
        "runs_passed": runs_passed,
        "cases": cases,
        "runs": runs,
        "note": (
            "Repeatability compares decoded-RGBA screen hashes, exit code and crash flag. "
            "Host-timed input remains approximate even when the hash repeats."
        ),
    }
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"repeatability: {'ok' if report['ok'] else 'diverged'} over {args.repeats} run(s)")
        for case_id, item in cases.items():
            print(f"  {case_id}: {'repeatable' if item['repeatable'] else 'diverged'}")
            if item.get("first_divergence"):
                print(f"    first divergence: {json.dumps(item['first_divergence'], sort_keys=True)}")
    return EXIT_OK if report["ok"] else EXIT_FAILED


def cmd_replay(args: argparse.Namespace) -> int:
    from .services.scenario import make_stdio_backend_factory

    factory = make_stdio_backend_factory(server_python=args.server_python, server_cmd=args.server_cmd)
    result = asyncio.run(
        replay_bundle(
            args.bundle,
            backend_factory=factory,
            artifacts_root=args.artifacts,
            timeout_ms=args.timeout_ms,
        )
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"replay: {'ok' if result['ok'] else 'diverged'} fidelity={result['fidelity']}")
        if result.get("first_divergence"):
            print(json.dumps(result["first_divergence"], indent=2, sort_keys=True))
        if result.get("artifacts"):
            print(f"artifacts: {result['artifacts']}")
    return EXIT_OK if result["ok"] else EXIT_FAILED


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m hatari_mcp.scenario_cli",
        description="Validate, run and replay Hatari MCP scenarios.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common_run_options(command: argparse.ArgumentParser) -> None:
        command.add_argument("--max-cases", type=int, default=None, help="Hard cap on expanded matrix cases.")
        command.add_argument("--fail-fast", action="store_true", help="Stop scheduling after the first failure.")
        command.add_argument("--jobs", type=int, default=1, help="Number of parallel case workers.")
        command.add_argument("--fail-on-skip", action="store_true", help="Treat skipped cases as failures.")
        command.add_argument("--server-python", default=None, help="Python executable for the MCP server.")
        command.add_argument("--server-cmd", default=None, help="Full MCP server command line.")
        command.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
        command.add_argument("--artifacts", default=None, help="Artifact root directory (default: /tmp).")

    validate = sub.add_parser("validate", help="Validate a scenario file without running it.")
    validate.add_argument("file")
    validate.add_argument("--max-cases", type=int, default=None)
    validate.add_argument("--server-python", default=None)
    validate.add_argument("--server-cmd", default=None)
    validate.add_argument("--json", action="store_true")
    validate.set_defaults(func=cmd_validate, fail_fast=False, jobs=1, fail_on_skip=False, artifacts=None, record=False)

    run = sub.add_parser("run", help="Run a scenario (optionally across its matrix).")
    run.add_argument("file")
    common_run_options(run)
    run.add_argument("--junit", default=None, help="Write JUnit XML to this path.")
    run.add_argument("--record", action="store_true", help="Record input events into replay/.")
    run.set_defaults(func=cmd_run)

    corpus = sub.add_parser("list-corpus", help="Show external corpus discovery and skip reasons.")
    corpus.add_argument("--json", action="store_true")
    corpus.set_defaults(func=cmd_list_corpus)

    repeat = sub.add_parser("repeat", help="Run a scenario N times and report the first divergent observable.")
    repeat.add_argument("file")
    repeat.add_argument("--repeats", type=int, default=3)
    common_run_options(repeat)
    repeat.set_defaults(func=cmd_repeat, record=False)

    replay = sub.add_parser("replay", help="Replay a recorded replay/ bundle.")
    replay.add_argument("bundle")
    replay.add_argument("--artifacts", default=None)
    replay.add_argument("--server-python", default=None)
    replay.add_argument("--server-cmd", default=None)
    replay.add_argument("--timeout-ms", type=int, default=120_000)
    replay.add_argument("--json", action="store_true")
    replay.set_defaults(func=cmd_replay)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED
    except ScenarioError as exc:
        _print_error(exc, as_json=getattr(args, "json", False))
        return EXIT_USAGE
    except HatariError as exc:
        _print_error(exc, as_json=getattr(args, "json", False))
        return EXIT_INFRA
    except Exception as exc:  # pragma: no cover - defensive
        _print_error(exc, as_json=getattr(args, "json", False))
        return EXIT_INTERNAL


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
