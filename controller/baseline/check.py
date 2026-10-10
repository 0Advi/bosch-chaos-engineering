"""Baseline gates: is a baseline usable for a run, and do repeats agree? (P1-03)"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from typing import Optional

from ..schemas import Baseline

CHECKOUT = "POST /api/checkout"


def load(path: Path) -> Baseline:
    return Baseline.model_validate_json(path.read_text())


def usable_blockers(path: Path, daemon_id: str, flag_file: Path, max_age_hours: float,
                    now: Optional[datetime] = None) -> list[str]:
    """Reasons this baseline cannot gate a live run (empty list = usable)."""
    if not path.is_file():
        return [f"baseline file not found: {path}"]
    try:
        baseline = load(path)
    except ValueError as error:
        return [f"baseline does not match the contract: {str(error)[:200]}"]
    blockers = []
    if not baseline.healthy:
        blockers.append(f"baseline {baseline.baseline_id} is not healthy: {baseline.blocking[:3]}")
    age = (now or datetime.now(timezone.utc)) - baseline.finished_at
    if age > timedelta(hours=max_age_hours):
        blockers.append(f"baseline is stale ({age.total_seconds() / 3600:.1f} h > {max_age_hours} h)")
    if baseline.sandbox.daemon_id != daemon_id:
        blockers.append("baseline was measured on a different Docker daemon")
    if baseline.flag_sha256 != hashlib.sha256(flag_file.read_bytes()).hexdigest():
        blockers.append("flag configuration changed since the baseline was measured")
    return blockers


def reproducibility_problems(a: Baseline, b: Baseline, rules: dict) -> list[str]:
    problems = []
    for baseline in (a, b):
        if not baseline.healthy:
            problems.append(f"{baseline.baseline_id} is not healthy")
    if a.workload.get("version") != b.workload.get("version") or \
            a.workload.get("product_id") != b.workload.get("product_id"):
        problems.append("baselines used different workloads")
    if a.sandbox.daemon_id != b.sandbox.daemon_id:
        problems.append("baselines were measured on different Docker daemons")
    if a.flag_sha256 != b.flag_sha256:
        problems.append("baselines used different flag configurations")
    pa, pb = a.probe(CHECKOUT), b.probe(CHECKOUT)
    if pa.p95_ms is None or pb.p95_ms is None:
        problems.append("checkout p95 missing from a baseline")
    else:
        ratio = max(pa.p95_ms, pb.p95_ms) / max(min(pa.p95_ms, pb.p95_ms), 1e-9)
        if ratio > rules["max_checkout_p95_ratio"]:
            problems.append(f"checkout p95 differs {ratio:.2f}x ({pa.p95_ms} vs {pb.p95_ms} ms)")
    if abs(pa.failure_rate - pb.failure_rate) > rules["max_failure_rate_delta"]:
        problems.append(f"checkout failure rates differ ({pa.failure_rate} vs {pb.failure_rate})")
    return problems
