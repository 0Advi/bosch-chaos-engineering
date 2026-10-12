"""Derive the payment execution policy from measured baselines (P1-04).

Nothing here reuses the early recommendation-experiment thresholds. Each
gate records its value, the rule that produced it and the measured inputs, so
every execution gate can be traced back to baseline evidence.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import itertools
import json
import math
from pathlib import Path

import yaml

from ..observer.fixtures import PROTECTED_PROBES
from ..schemas import Baseline, Gate, PaymentPolicy
from .check import CHECKOUT, reproducibility_problems

ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = ROOT / "config" / "payment_policy.yaml"
FAULT_HARD_CAP_SECONDS = 30      # plan-wide cap (P2-04), not derived
MIN_AFFECTED_SAMPLES = 5         # fewer checkout samples during a fault = inconclusive
RECOVERY_CHECKS = 3


class PolicyError(ValueError):
    """Raised when a payment policy cannot be derived from the given baselines."""
    pass


def _ceil_half(seconds: float) -> float:
    """Round seconds up to the next half second."""
    return math.ceil(seconds * 2) / 2


def _worst(baselines: list[Baseline], probe: str, field: str) -> float:
    """The worst (largest) value of a probe statistic across baselines."""
    values = [getattr(b.probe(probe), field) for b in baselines]
    if any(v is None for v in values):
        raise PolicyError(f"{probe} {field} missing from a baseline")
    return max(values)


def derive(baselines: list[Baseline], criteria: dict, sources: list[Path]) -> PaymentPolicy:
    """Derive the payment execution policy (abort and recovery thresholds) from at least two
    reproducible healthy baselines, recording each gate's rule and inputs.
    """
    if len(baselines) < 2:
        raise PolicyError("at least two healthy baselines are required for reproducibility")
    problems = [p for a, b in itertools.combinations(baselines, 2)
                for p in reproducibility_problems(a, b, criteria["reproducibility"])]
    if problems:
        raise PolicyError("baselines are not reproducible: " + "; ".join(sorted(set(problems))))

    ids = [b.baseline_id for b in baselines]
    p95, p99 = _worst(baselines, CHECKOUT, "p95_ms"), _worst(baselines, CHECKOUT, "p99_ms")
    prot_p95 = max(_worst(baselines, p, "p95_ms") for p in PROTECTED_PROBES)
    prot_p99 = max(_worst(baselines, p, "p99_ms") for p in PROTECTED_PROBES)
    prot_fail = max(b.probe(p).failure_rate for b in baselines for p in PROTECTED_PROBES)
    delays = [b.telemetry["trace_ingest_delay_s"]["p95"] for b in baselines
              if b.telemetry.get("trace_ingest_delay_s", {}).get("p95") is not None]
    metric_ages = [m["age_seconds"] for b in baselines
                   for m in b.telemetry.get("metric_freshness", {}).values() if m.get("age_seconds") is not None]
    limitations = [f"Derived from {len(baselines)} baselines on daemon {baselines[0].sandbox.daemon_id or 'unrecorded'}; "
                   "re-derive after host, image, workload or flag changes."]

    def gate(value, unit, rule, **inputs):
        """Build one policy gate with its value, unit, rule and the baselines it came from."""
        return Gate(value=value, unit=unit, rule=rule, inputs={"baselines": ids, **inputs})

    checkout_timeout = _ceil_half(max(2 * p99, 3 * p95) / 1000)
    probe_interval = _ceil_half(p95 / 1000)
    needed = MIN_AFFECTED_SAMPLES * probe_interval
    duration = min(FAULT_HARD_CAP_SECONDS, max(15, math.ceil(needed)))
    if needed > FAULT_HARD_CAP_SECONDS:
        limitations.append(f"{MIN_AFFECTED_SAMPLES} checkout samples need {needed:.0f} s, above the "
                           f"{FAULT_HARD_CAP_SECONDS} s cap; such runs will be inconclusive.")
    protected_min_samples = len(PROTECTED_PROBES) * MIN_AFFECTED_SAMPLES
    if delays:
        obs_age = max(5, math.ceil(2 * max(delays)))
        obs_rule = "max(5, 2 x worst p95 trace ingestion delay)"
    else:
        obs_age = 30
        obs_rule = "no ingestion delay measured; conservative 30 s placeholder"
        limitations.append("Trace ingestion delay was not measured; observation_max_age is a placeholder.")
    if not metric_ages:
        raise PolicyError("no metric freshness was measured")

    gates = {
        "checkout_probe_timeout_seconds": gate(
            checkout_timeout, "s", "max(2 x p99, 3 x p95) of valid checkout, rounded up to 0.5 s",
            checkout_p95_ms=p95, checkout_p99_ms=p99),
        "protected_probe_timeout_seconds": gate(
            max(1.0, _ceil_half(3 * prot_p99 / 1000)), "s", "max(1 s, 3 x worst protected-probe p99)",
            protected_p99_ms=prot_p99),
        "probe_interval_seconds": gate(
            probe_interval, "s", "checkout p95 rounded up to 0.5 s (one sequential probe per interval)",
            checkout_p95_ms=p95),
        "min_affected_samples": gate(
            MIN_AFFECTED_SAMPLES, "samples", "fixed design minimum; fewer checkout samples = inconclusive"),
        "recommended_fault_duration_seconds": gate(
            duration, "s", f"clamp(min_affected_samples x probe_interval, 15, {FAULT_HARD_CAP_SECONDS})",
            needed_seconds=needed),
        "fault_hard_cap_seconds": gate(FAULT_HARD_CAP_SECONDS, "s", "plan-wide cap; not derived"),
        "protected_min_samples": gate(
            protected_min_samples, "samples", "protected probes per checkout probe x min_affected_samples",
            probes=list(PROTECTED_PROBES)),
        "protected_max_error_rate": gate(
            round(max(2 * prot_fail, 1 / protected_min_samples), 4), "ratio",
            "max(2 x worst baseline protected failure rate, one failed sample per protected_min_samples)",
            baseline_protected_failure_rate=prot_fail),
        "protected_p95_latency_ms": gate(
            math.ceil(max(2 * prot_p95, 1.5 * prot_p99)), "ms", "max(2 x worst protected p95, 1.5 x worst p99)",
            protected_p95_ms=prot_p95, protected_p99_ms=prot_p99),
        "observation_max_age_seconds": gate(obs_age, "s", obs_rule, trace_ingest_delay_p95_s=delays),
        "metric_max_age_seconds": gate(
            math.ceil(1.5 * max(metric_ages)), "s", "1.5 x oldest metric sample age seen in healthy baselines",
            metric_ages_s=metric_ages),
        "recovery_checks": gate(RECOVERY_CHECKS, "checkouts",
                                "valid checkouts that must all match expectations after restoration"),
        "restoration_deadline_seconds": gate(
            math.ceil(RECOVERY_CHECKS * checkout_timeout + obs_age), "s",
            "recovery_checks x checkout_probe_timeout + observation_max_age",
            checkout_timeout_s=checkout_timeout, observation_max_age_s=obs_age),
    }
    limitations.append("Flag propagation latency is not measured in P1; P2-02 must verify it per run.")
    digest = hashlib.sha256(json.dumps({k: g.value for k, g in gates.items()}, sort_keys=True).encode()
                            + "".join(ids).encode()).hexdigest()[:8]
    now = datetime.now(timezone.utc)
    return PaymentPolicy(
        policy_version=f"payment-{now:%Y%m%d}-{digest}", derived_at=now,
        derived_from=[{"baseline_id": b.baseline_id, "file": str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p),
                       "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for b, p in zip(baselines, sources)],
        affected_probes=[CHECKOUT], protected_probes=list(PROTECTED_PROBES), gates=gates,
        limitations=limitations)


def write(policy: PaymentPolicy, path: Path = POLICY_PATH) -> Path:
    """Write the derived policy to config/payment_policy.yaml with a do-not-edit header."""
    header = ("# Generated by: python -m controller.cli derive-policy (P1-04). Do not edit by hand;\n"
              "# re-derive from new baselines instead. Each gate lists its rule and measured inputs.\n")
    path.write_text(header + yaml.safe_dump(policy.model_dump(mode="json"), sort_keys=False))
    return path


def load(path: Path = POLICY_PATH) -> PaymentPolicy:
    """Load the derived payment policy."""
    return PaymentPolicy.model_validate(yaml.safe_load(path.read_text()))
