import os

os.environ.setdefault("HATARI_MCP_LABS", "1")

from labs.stats import (  # noqa: E402
    detect_regression,
    minimum_detectable_ratio,
    permutation_test,
    summary,
)


def test_summary_reports_cov_and_median():
    result = summary([1.0, 1.0, 2.0, 2.0])
    assert result["n"] == 4
    assert result["median"] == 1.5
    assert result["cov"] > 0


def test_permutation_detects_large_shift():
    baseline = [0.430, 0.429, 0.431, 0.430, 0.429, 0.430, 0.431, 0.429, 0.430, 0.430]
    candidate = [value * 1.12 for value in baseline]
    result = permutation_test(baseline, candidate, iterations=2000)
    assert result["significant"] is True
    assert result["p_value"] < 0.05


def test_detect_regression_respects_threshold():
    baseline = [0.430, 0.429, 0.431, 0.430, 0.429, 0.430, 0.431, 0.429, 0.430, 0.430]
    small = [value * 1.01 for value in baseline]
    result = detect_regression(baseline, small, iterations=2000)
    assert result["detected"] is False
    assert result["median_ratio"] < 1.05


def test_minimum_detectable_ratio_scales_with_cov():
    low = minimum_detectable_ratio([1.0, 1.001, 0.999, 1.0], 10)
    high = minimum_detectable_ratio([1.0, 1.2, 0.8, 1.0], 10)
    assert low < high
