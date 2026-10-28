import os

os.environ.setdefault("HATARI_MCP_LABS", "1")

from labs import bench_spike  # noqa: E402
from labs.stats import detect_regression  # noqa: E402


def test_interleaved_order_is_balanced_and_reproducible():
    first = bench_spike.interleaved_order(10, seed=8128)
    second = bench_spike.interleaved_order(10, seed=8128)
    assert first == second
    assert len(first) == 20
    assert first.count("baseline") == 10
    assert first.count("injected") == 10
    assert first != ["baseline", "injected"] * 10


def test_detection_flags_a_real_shift_and_ignores_noise():
    baseline = [0.430, 0.429, 0.431, 0.430, 0.429, 0.430, 0.431, 0.429, 0.430, 0.430]
    injected = [value * 1.12 for value in baseline]
    assert detect_regression(baseline, injected, iterations=2000)["detected"] is True
    noisy = [0.430, 0.429, 0.431, 0.430, 0.429, 0.430, 0.431, 0.429, 0.430, 0.430]
    assert detect_regression(baseline, noisy, iterations=2000)["detected"] is False


def _detection():
    return {"median_ratio": 1.12, "p_value": 0.001, "detected": True}


def test_gate_requires_exactly_three_profiles_and_ten_reps():
    assert bench_spike.benchmark_design_ok(("st", "ste", "falcon"), 10) is True
    assert bench_spike.benchmark_design_ok(("st", "ste"), 10) is False
    assert bench_spike.benchmark_design_ok(("st", "ste", "falcon"), 9) is False
    summary = bench_spike.gate_summary(
        profiles=("st", "ste"),
        reps=10,
        max_cov=0.01,
        detection=_detection(),
        workload_returncodes=[0, 0],
        interactive_returncodes=[0, 0],
        dedicated_runner=True,
    )
    assert summary["design_ok"] is False
    assert summary["go"] is False


def test_gate_rejects_nonzero_interactive_returncode():
    summary = bench_spike.gate_summary(
        profiles=("st", "ste", "falcon"),
        reps=10,
        max_cov=0.01,
        detection=_detection(),
        workload_returncodes=[0] * 30,
        interactive_returncodes=[0, 0, 1],
        dedicated_runner=True,
    )
    assert summary["interactive_returncodes_ok"] is False
    assert summary["returncodes_ok"] is False
    assert summary["go"] is False


def test_gate_requires_dedicated_runner():
    summary = bench_spike.gate_summary(
        profiles=("st", "ste", "falcon"),
        reps=10,
        max_cov=0.01,
        detection=_detection(),
        workload_returncodes=[0] * 30,
        interactive_returncodes=[0] * 30,
        dedicated_runner=False,
    )
    assert summary["go_harness_feasibility"] is True
    assert summary["go"] is False
