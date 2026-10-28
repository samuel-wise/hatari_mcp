"""Statistics helpers for the P8 benchmark and regression spike.

Only the standard library is used so the lab package has no production or
optional dependencies.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Iterable, Sequence


def summary(values: Iterable[float]) -> dict[str, float | int]:
    data = [float(v) for v in values]
    if not data:
        return {"n": 0}
    mean = statistics.fmean(data)
    median = statistics.median(data)
    stdev = statistics.stdev(data) if len(data) > 1 else 0.0
    ordered = sorted(data)
    p95 = ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)]
    return {
        "n": len(data),
        "mean": mean,
        "median": median,
        "stdev": stdev,
        "cov": (stdev / mean) if mean else 0.0,
        "min": ordered[0],
        "max": ordered[-1],
        "p95": p95,
    }


def median_ratio(baseline: Sequence[float], candidate: Sequence[float]) -> float:
    base = statistics.median(baseline)
    return statistics.median(candidate) / base if base else math.inf


def permutation_test(
    baseline: Sequence[float],
    candidate: Sequence[float],
    *,
    iterations: int = 20000,
    seed: int = 8128,
) -> dict[str, float | int | bool]:
    a = [float(v) for v in baseline]
    b = [float(v) for v in candidate]
    if not a or not b:
        return {"observed": 0.0, "p_value": 1.0, "iterations": 0, "significant": False}
    observed = statistics.median(b) - statistics.median(a)
    pooled = a + b
    rng = random.Random(seed)
    hits = 0
    n_a = len(a)
    for _ in range(iterations):
        rng.shuffle(pooled)
        delta = statistics.median(pooled[n_a:]) - statistics.median(pooled[:n_a])
        if abs(delta) >= abs(observed):
            hits += 1
    p_value = (hits + 1) / (iterations + 1)
    return {
        "observed": observed,
        "p_value": p_value,
        "iterations": iterations,
        "significant": p_value < 0.05,
    }


def bootstrap_median_ci(
    values: Sequence[float],
    *,
    iterations: int = 4000,
    seed: int = 8128,
    confidence: float = 0.95,
) -> dict[str, float]:
    data = [float(v) for v in values]
    if not data:
        return {"low": 0.0, "high": 0.0}
    rng = random.Random(seed)
    medians = []
    for _ in range(iterations):
        sample = [data[rng.randrange(len(data))] for _ in data]
        medians.append(statistics.median(sample))
    medians.sort()
    alpha = (1.0 - confidence) / 2.0
    low = medians[int(alpha * len(medians))]
    high = medians[min(len(medians) - 1, int((1.0 - alpha) * len(medians)))]
    return {"low": low, "high": high}


def detect_regression(
    baseline: Sequence[float],
    candidate: Sequence[float],
    *,
    min_ratio: float = 1.05,
    alpha: float = 0.05,
    iterations: int = 20000,
) -> dict[str, float | bool]:
    ratio = median_ratio(baseline, candidate)
    test = permutation_test(baseline, candidate, iterations=iterations)
    detected = bool(ratio >= min_ratio and float(test["p_value"]) < alpha)
    return {
        "median_ratio": ratio,
        "p_value": float(test["p_value"]),
        "min_ratio": min_ratio,
        "alpha": alpha,
        "detected": detected,
    }


def minimum_detectable_ratio(values: Sequence[float], n: int, z: float = 2.8) -> float:
    stats = summary(values)
    cov = float(stats.get("cov", 0.0))
    if cov == 0.0 or n <= 0:
        return 0.0
    return z * cov * math.sqrt(2.0 / n)
