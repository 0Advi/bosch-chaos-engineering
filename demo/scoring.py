"""Deterministic resilience score (0-100) for one experiment, with its breakdown.

  availability   30%  mean success rate across user paths while the fault was active
  containment    25%  share of endpoints that do NOT depend on the target and stayed healthy
  recovery       20%  time from restore to fully healthy: <=3 s = 100, >=60 s or never = 0
  error handling 15%  failures that were fast and honest (5xx < 2 s) vs timeouts or
                      misleading 4xx blaming the customer for a server fault
  latency        10%  median of successful requests under fault vs baseline
"""
from __future__ import annotations

WEIGHTS = {"availability": 0.30, "containment": 0.25, "recovery": 0.20, "error_handling": 0.15, "latency": 0.10}


def _p95(values):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]


def _p50(values):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def score(records: list[dict], blast: dict, time_to_recover_s: float | None, aborted: bool) -> dict:
    fault = [r for r in records if r["phase"] == "fault"]
    base = [r for r in records if r["phase"] == "baseline"]
    # Mean per-path success, so a path whose requests hang (few samples) weighs as much as a fast one.
    per_path = {}
    for r in fault:
        per_path.setdefault(r["endpoint"], []).append(r["ok"])
    availability = 100 * sum(sum(v) / len(v) for v in per_path.values()) / len(per_path) if per_path else 0

    independent = [e for e in blast["healthy_endpoints"] + list(blast["impacted_endpoints"])
                   if e not in blast["expected_endpoints"]]
    containment = 100 * (1 - len(blast["unexpected_endpoints"]) / len(independent)) if independent else 100
    if aborted:
        containment = min(containment, 40)

    if time_to_recover_s is None:
        recovery = 0
    else:
        recovery = max(0, min(100, 100 * (60 - time_to_recover_s) / 57))

    failures = [r for r in fault if not r["ok"]]
    if failures:
        honest = sum(1 for r in failures if not r["timeout"] and r["status"] and r["status"] >= 500
                     and (r["latency_ms"] or 0) < 2000)
        misleading = sum(1 for r in failures if r["status"] and 400 <= r["status"] < 500)
        timeouts = sum(1 for r in failures if r["timeout"] or (r["latency_ms"] or 0) >= 5000)
        error_handling = 100 * honest / len(failures)
    else:
        misleading = timeouts = 0
        error_handling = 100

    b95 = _p95([r["latency_ms"] for r in base if r["ok"] and r["latency_ms"]])
    f95 = _p95([r["latency_ms"] for r in fault if r["ok"] and r["latency_ms"]])
    b50 = _p50([r["latency_ms"] for r in base if r["ok"] and r["latency_ms"]])
    f50 = _p50([r["latency_ms"] for r in fault if r["ok"] and r["latency_ms"]])
    if not b50 or not f50:
        latency = 100 if f50 is None and not fault else 50
    else:
        ratio = f50 / b50
        latency = max(0, min(100, 100 - (ratio - 1.5) * 25)) if ratio > 1.5 else 100

    parts = {"availability": availability, "containment": containment, "recovery": recovery,
             "error_handling": error_handling, "latency": latency}
    total = sum(WEIGHTS[k] * v for k, v in parts.items())
    return {"score": round(total), "grade": grade(total),
            "parts": {k: round(v) for k, v in parts.items()}, "weights": WEIGHTS,
            "facts": {"fault_requests": len(fault), "fault_failures": len(failures),
                      "misleading_4xx": misleading, "timeouts": timeouts,
                      "baseline_p95_ms": b95, "fault_p95_ms": f95}}


def grade(total: float) -> str:
    return "A" if total >= 90 else "B" if total >= 75 else "C" if total >= 60 else "D" if total >= 40 else "F"
