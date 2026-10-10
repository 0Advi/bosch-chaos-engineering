"""Small, dependency-free statistics for latency and failure summaries."""
from __future__ import annotations

import math
from typing import Iterable, Optional

from ..schemas import ProbeStats


def percentile(values: list[float], p: float) -> Optional[float]:
    """Nearest-rank percentile; None for an empty sample (never invented)."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


def probe_stats(probe: str, samples: Iterable[tuple[bool, Optional[float]]]) -> ProbeStats:
    """samples: (ok, latency_ms or None). Every sample counts as an attempt."""
    samples = list(samples)
    latencies = [lat for _, lat in samples if lat is not None]
    failures = sum(1 for ok, _ in samples if not ok)
    return ProbeStats(
        probe=probe, attempts=len(samples), failures=failures,
        failure_rate=round(failures / len(samples), 4) if samples else 0.0,
        p50_ms=percentile(latencies, 50), p90_ms=percentile(latencies, 90),
        p95_ms=percentile(latencies, 95), p99_ms=percentile(latencies, 99),
        max_ms=max(latencies) if latencies else None)
