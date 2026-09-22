"""Seven-run same-host CLI latency comparison against the pinned 1.0.0 entry."""
from __future__ import annotations

import statistics
import time
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class LatencyResult:
    baseline_median_ns: int
    current_median_ns: int
    ratio: float
    verdict: str


def measure_seven(operation: Callable[[], object]) -> tuple[int, ...]:
    operation()  # warm once on the same host
    values: list[int] = []
    for _ in range(7):
        start = time.perf_counter_ns()
        operation()
        values.append(time.perf_counter_ns() - start)
    return tuple(values)


def compare_latency(
    baseline_samples: tuple[int, ...], current_samples: tuple[int, ...]
) -> LatencyResult:
    for samples in (baseline_samples, current_samples):
        if (not isinstance(samples, tuple) or len(samples) != 7
                or any(type(value) is not int or value <= 0 for value in samples)):
            raise ValueError("latency comparison requires seven positive samples")
    old = int(statistics.median(baseline_samples))
    new = int(statistics.median(current_samples))
    ratio = new / old
    if ratio >= 10 and new >= 20_000_000:
        verdict = "BLOCK"
    elif ratio >= 2:
        verdict = "FINDING"
    else:
        verdict = "PASS"
    return LatencyResult(old, new, ratio, verdict)
