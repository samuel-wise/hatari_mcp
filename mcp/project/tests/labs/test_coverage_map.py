import os
from pathlib import Path

os.environ.setdefault("HATARI_MCP_LABS", "1")

import pytest

from labs import coverage_gap_spike  # noqa: E402

PROJECT = Path(__file__).resolve().parents[2]
HELLO = PROJECT / "tools/ahcc/ahcc/HELLO"
HELLO_REQUIRED = pytest.mark.skipif(
    not (HELLO / "HELLO_ST.MAP").is_file() or not (HELLO / "HELLO_ST.TOS").is_file(),
    reason="AHCC HELLO_ST corpus is not provisioned (tools/ahcc is gitignored)",
)

SYNTHETIC_MAP = """\
* synthetic map
       0      0[   460]  TEXT
     460      0[    16]  TEXT main
     476      0[    24]  TEXT isdigit
     500     24[    24]  TEXT isupper
     524     48[    66]  TEXT ultoa
     900    424[   134]  TEXT <_fflush>
    1034    558[    86]  TEXT fflush
    1120    644[   116]  TEXT fclose
    1802   1326[   866]  TEXT _printf
    2688   2212[    32]  TEXT printf
"""


def _map_file(tmp_path: Path) -> Path:
    path = tmp_path / "HELLO_ST.MAP"
    path.write_text(SYNTHETIC_MAP, encoding="latin-1")
    return path


def test_map_parser_and_function_assignment(tmp_path):
    functions = coverage_gap_spike.load_map(_map_file(tmp_path))
    names = {item["name"] for item in functions}
    assert {"main", "_printf"} <= names
    counts = coverage_gap_spike.assign_functions([460, 461, 462], functions)
    assert counts["main"] == 3


def test_profile_parser_and_zero_sample_detection(tmp_path):
    profile = tmp_path / "profile.txt"
    profile.write_text(
        "PROGRAM_TEXT:\t0x00f176-0x010528\n"
        "0000f342 41f9 0001 0542 lea.l $00010542,a0 0.01% (4, 20, 0, 0)\n",
        encoding="latin-1",
    )
    parsed = coverage_gap_spike.parse_profile(profile)
    assert parsed["base"] == 0xF176
    assert parsed["end"] == 0x010528
    assert parsed["instructions"] == 4
    functions = coverage_gap_spike.load_map(_map_file(tmp_path))
    counts = coverage_gap_spike.assign_functions(parsed["offsets"], functions)
    assert counts["main"] == 1
    assert "isdigit" not in counts


def test_trace_parser_infers_program_base(tmp_path):
    trace = tmp_path / "trace.txt"
    trace.write_text(
        "00e00010 4e71 nop\n0000f176 2648 movea.l a0,a3\n0000f178 200b move.l a3,d0\n",
        encoding="latin-1",
    )
    parsed = coverage_gap_spike.parse_trace(trace)
    assert parsed["base"] == 0xF176
    assert parsed["base_source"] == "inferred"
    assert parsed["unique_pcs"] == 3
    assert 0 in parsed["offsets"]


def test_trace_explicit_base_avoids_fragile_inference(tmp_path):
    trace = tmp_path / "trace.txt"
    trace.write_text(
        "00e00010 4e71 nop\n0000f176 2648 movea.l a0,a3\n0000f178 200b move.l a3,d0\n",
        encoding="latin-1",
    )
    parsed = coverage_gap_spike.parse_trace(trace, base=0xF176)
    assert parsed["base"] == 0xF176
    assert parsed["base_source"] == "explicit"
    assert parsed["offsets"] == [0, 2]


def test_empty_profile_and_trace_are_no_go():
    summary = coverage_gap_spike.build_summary({"profile_runs": [], "trace_runs": []})
    assert summary["go_function_level"] is False
    assert summary["verdict"].startswith("NO-GO")
    assert summary["profile_stable"] is False


def test_zero_data_runs_are_no_go():
    empty_run = {
        "unique_pcs": 0,
        "instructions": 0,
        "active_functions": [],
        "function_counts": {},
    }
    summary = coverage_gap_spike.build_summary(
        {"profile_runs": [empty_run] * 3, "trace_runs": [empty_run] * 3}
    )
    assert summary["go_function_level"] is False
    assert summary["verdict"].startswith("NO-GO")
    assert summary["profile_valid"] is False
    assert summary["trace_valid"] is False


def test_hit_counts_not_zero_zero_presence_drive_agreement():
    profile = {
        "unique_pcs": 10,
        "instructions": 10,
        "active_functions": ["main", "fclose"],
        "function_counts": {"main": 4, "fclose": 25},
    }
    trace = {
        "trace_lines": 100,
        "unique_pcs": 50,
        "active_functions": ["main", "fclose"],
        "function_counts": {"main": 4, "fclose": 2},
    }
    summary = coverage_gap_spike.build_summary(
        {
            "profile_runs": [profile] * 3,
            "trace_runs": [trace] * 3,
            "zero_sample_functions": ["isdigit", "isupper", "ultoa", "ltoa", "fflush"],
        },
        sample_size=10,
    )
    assert summary["sample_agreement"] < 1.0
    assert all(not row["zero_zero"] for row in summary["sample"] if row["function"] in {"main", "fclose"})
    assert summary["sample_active_rows"] == 2


@HELLO_REQUIRED
def test_real_map_has_expected_symbols():
    functions = coverage_gap_spike.load_map(HELLO / "HELLO_ST.MAP")
    names = {item["name"] for item in functions}
    assert {"main", "printf", "_printf"} <= names


def _positive_runs(counts):
    run = {
        "unique_pcs": 100,
        "instructions": 1000,
        "active_functions": sorted(counts),
        "function_counts": dict(counts),
    }
    return run


def _complete_evidence(counts, *, gap_function="gap_fn"):
    profile = _positive_runs(counts)
    trace = _positive_runs(counts)
    profile_with_cpu = dict(profile, cpu_s=1.0)
    baseline_with_cpu = {"elapsed_s": 1.0, "cpu_s": 1.0}
    return {
        "profile_runs": [profile_with_cpu] * 3,
        "trace_runs": [trace] * 3,
        "baseline_runs": [baseline_with_cpu] * 3,
        "zero_sample_functions": [gap_function],
        "scenario_step_gap_map": [
            {"function": gap_function, "scenario_step": "scenario:boot"}
        ],
        "p6_11_integration": {
            "status": "supported",
            "backend": "p6.11",
            "scenario_id": "hello-profile",
            "artifact": "/tmp/p6/artifact.json",
            "profile_run_ids": ["p1", "p2", "p3"],
            "trace_run_ids": ["t1", "t2", "t3"],
            "sample_count": 1000,
        },
    }


def test_fabricated_positive_counts_and_manual_review_do_not_go():
    counts = {f"f{index}": index + 1 for index in range(10)}
    data = {
        "profile_runs": [_positive_runs(counts)] * 3,
        "trace_runs": [_positive_runs(counts)] * 3,
        "zero_sample_functions": ["gap_fn"],
    }
    summary = coverage_gap_spike.build_summary(
        data, sample_size=10, manual_review={"done": True}
    )
    assert summary["go_function_level"] is False
    assert summary["p6_11_integration"] is False
    assert summary["overhead_ok"] is False
    assert summary["scenario_step_gap_mapping_ok"] is False
    assert summary["verdict"].startswith("INCONCLUSIVE")


def test_function_level_go_requires_p6_11_overhead_and_gap_mapping():
    counts = {f"f{index}": index + 1 for index in range(10)}
    data = _complete_evidence(counts)
    summary = coverage_gap_spike.build_summary(
        data, sample_size=10, manual_review={"done": True}
    )
    assert summary["go_function_level"] is True
    assert summary["p6_11_integration"] is True
    assert summary["overhead_ok"] is True
    assert summary["scenario_step_gap_mapping_ok"] is True

    missing_gap = _complete_evidence(counts)
    missing_gap.pop("scenario_step_gap_map")
    summary = coverage_gap_spike.build_summary(
        missing_gap, sample_size=10, manual_review={"done": True}
    )
    assert summary["go_function_level"] is False


def test_coverage_cli_writes_out_dir_coverage_json(tmp_path):
    from labs import __main__ as labs_cli

    retained = tmp_path / "retained.json"
    retained.write_text("{}", encoding="utf-8")
    out = tmp_path / "out"
    assert labs_cli.main(["coverage", "--from-json", str(retained), "--out", str(out)]) == 0
    assert (out / "coverage.json").is_file()
    assert not (out / "coverage" / "coverage.json").exists()
