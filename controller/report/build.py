"""Build a RunReport from a run record and its events only (P3-01).

Pure and deterministic: no clock, no network, no model. The same inputs,
whether read from the run store or from exported files, give the same report.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional

from ..baseline.check import CHECKOUT
from ..baseline.stats import percentile
from ..schemas import (Baseline, Comparison, Event, RunReport, RunState as S, TERMINAL,
                       TimelineEntry)
from ..schemas.contracts import PhaseSummary

ROOT = Path(__file__).resolve().parents[2]
REPORT_LIMITATIONS = [
    "Fault-phase probes record HTTP outcome and latency; trace/log correlation uses the P1 observer.",
    "Values describe one sandbox run on one host and are not a production estimate.",
]


def canonical_event(event: Event) -> str:
    """The exact line written to events.jsonl; digests are computed over these."""
    return json.dumps(event.model_dump(mode="json"), sort_keys=True)


def events_digest(events: list[Event]) -> str:
    return hashlib.sha256("\n".join(canonical_event(e) for e in events).encode()).hexdigest()


def _checkout(events: list[Event], phase: str) -> list[Event]:
    return [e for e in events if e.type == "observation" and e.data.get("phase") == phase
            and e.data.get("probe") == CHECKOUT]


def _phase(samples: list[Event], expected: int) -> PhaseSummary:
    latencies = [e.data["latency_ms"] for e in samples if e.data.get("latency_ms") is not None]
    return PhaseSummary(attempts=len(samples), successes=sum(1 for e in samples if e.data.get("matched")),
                        missing_samples=max(0, expected - len(samples)),
                        p95_latency_ms=percentile(latencies, 95))


def _fixture_phase(events: list[Event]) -> PhaseSummary:
    items = [e for e in events if e.type == "observation" and e.data.get("phase") == "fault"
             and e.data.get("probe") is None and e.data.get("kind") is None]
    ok = sum(1 for e in items if e.data.get("http_status") == 200 or e.data.get("exit_code") == 0)
    return PhaseSummary(attempts=len(items), successes=ok, missing_samples=0 if items else 1)


def _cleanup_status(state: S, states_seen: set[S], recovered: bool) -> str:
    if state == S.NEEDS_ATTENTION:
        return "needs_attention"
    mutated = S.ARMED in states_seen
    if state in TERMINAL:
        if not mutated:
            return "not_required"
        return "verified" if recovered else "needs_attention"
    return "pending" if mutated else "not_started"


def _baseline_reference(ref: Optional[str], root: Path, missing: list[str]) -> Optional[dict]:
    if not ref:
        missing.append("plan has no baseline_ref")
        return None
    path = root / ref
    if not path.is_file():
        missing.append(f"baseline file not available: {ref}")
        return None
    raw = path.read_bytes()
    baseline = Baseline.model_validate_json(raw)
    checkout = baseline.probe(CHECKOUT)
    return {"baseline_id": baseline.baseline_id, "file": ref, "sha256": hashlib.sha256(raw).hexdigest(),
            "healthy": baseline.healthy, "checkout_attempts": checkout.attempts,
            "checkout_failure_rate": checkout.failure_rate, "checkout_p95_ms": checkout.p95_ms}


def _rate(summary: PhaseSummary) -> str:
    return f"{summary.successes}/{summary.attempts}" if summary.attempts else "no samples"


def build_report(run: dict, events: list[Event], root: Path = ROOT) -> RunReport:
    events = sorted(events, key=lambda e: e.seq)
    run_id, plan = run["run_id"], run["plan"] or {}
    state = S(run["state"])
    result = run.get("result") or {}
    limits = ((run.get("recovery") or {}).get("limits")) or {}
    missing: list[str] = []
    limitations = list(REPORT_LIMITATIONS)

    transitions = [e for e in events if e.type == "state_changed"]
    timeline = [TimelineEntry(seq=e.seq, at=e.at, state=e.state, stop_reason=e.stop_reason,
                              by=e.data.get("by")) for e in transitions]
    states_seen = {S.DRAFT, *(t.state for t in timeline)}
    recovered = any(e.data.get("recovered") for e in transitions)

    applied = next((e for e in events if e.type == "action_applied"), None)
    restored = next((e for e in events if e.type == "action_restored"), None)
    evaluation = next((e for e in events if e.type == "observation" and e.data.get("kind") == "flag_evaluation"), None)
    injection = None
    if applied:
        injection = {"flag": applied.data.get("flag"), "variant": applied.data.get("variant"),
                     "applied_at": applied.at.isoformat(), "applied_seq": applied.seq,
                     "served_value": evaluation.data.get("served") if evaluation else None,
                     "evaluation_matches": evaluation.data.get("matches") if evaluation else None,
                     "restored_at": restored.at.isoformat() if restored else None,
                     "restore_outcome": restored.data.get("file") if restored else None,
                     "restore_evaluation_matches": restored.data.get("evaluation_matches") if restored else None,
                     "restorer": run.get("restorer")}
        if evaluation is None:
            missing.append("no flagd evaluation recorded after injection")
        if restored is None and state in TERMINAL:
            missing.append("injection applied but no restoration event recorded")
    restorer = run.get("restorer") or ""
    if restorer.startswith("watchdog:"):
        limitations.append("Restoration was performed by the watchdog, not the controller.")

    is_fixture = applied is None and "fixture_results" in result
    baseline_phase = _phase(_checkout(events, "baseline"), expected=1 if S.BASELINING in states_seen else 0)
    fault_phase = (_fixture_phase(events) if is_fixture else
                   _phase(_checkout(events, "fault"), expected=int(limits.get("min_affected_samples", 0))
                          if applied else 0))
    recovery_phase = _phase(_checkout(events, "recovery"),
                            expected=int(limits.get("recovery_checks", 0)) if S.VERIFYING_RECOVERY in states_seen else 0)
    if fault_phase.missing_samples:
        missing.append(f"fault phase is {fault_phase.missing_samples} sample(s) short of the policy minimum")
    if recovery_phase.missing_samples:
        missing.append(f"recovery phase is missing {recovery_phase.missing_samples} check(s)")
    if not result:
        missing.append("run result was not recorded")

    protected_events = [e for e in events if e.type == "observation" and e.data.get("kind") == "protected"]
    protected = {"samples": sum(e.data.get("samples", 0) for e in protected_events),
                 "failures": sum(len(e.data.get("failed", [])) for e in protected_events),
                 "failed_probes": sorted({p for e in protected_events for p in e.data.get("failed", [])})}

    differences = [f"valid checkout: baseline {_rate(baseline_phase)}, fault {_rate(fault_phase)}, "
                   f"recovery {_rate(recovery_phase)}"]
    if baseline_phase.p95_latency_ms is not None and fault_phase.p95_latency_ms is not None:
        differences.append(f"checkout p95: baseline {baseline_phase.p95_latency_ms} ms, "
                           f"fault {fault_phase.p95_latency_ms} ms")
    if protected["samples"]:
        differences.append(f"protected probes: {protected['failures']} failed of {protected['samples']}")

    conclusive = bool(result.get("conclusive")) and state in TERMINAL
    comparison = Comparison(comparison_id=f"cmp-{run_id}"[:64], original_run_id=run_id,
                            baseline=baseline_phase, fault=fault_phase, recovery=recovery_phase,
                            differences=differences, limitations=limitations, conclusive=conclusive)
    digest = events_digest(events)
    return RunReport(
        report_id="rpt-" + hashlib.sha256(f"{run_id}:{digest}".encode()).hexdigest()[:16],
        run_id=run_id, capability_id=run["capability_id"],
        plan={k: plan.get(k) for k in ("plan_id", "request_id", "parameters", "duration_seconds",
                                       "baseline_ref", "requested_by", "policy_version")},
        state=state, stop_reason=run.get("stop_reason"), terminal=state in TERMINAL,
        cleanup_status=_cleanup_status(state, states_seen, recovered), timeline=timeline,
        injection=injection, comparison=comparison, protected=protected,
        baseline_reference=_baseline_reference(plan.get("baseline_ref"), root, missing),
        conclusive=conclusive, impact_observed=result.get("impact_observed"),
        missing_data=missing, limitations=limitations,
        evidence={"event_count": len(events), "last_seq": events[-1].seq if events else 0,
                  "events_sha256": digest})
