"""Readable Markdown for a RunReport (deterministic; no timestamps of its own)."""
from __future__ import annotations

from ..schemas import RunReport
from ..schemas.contracts import PhaseSummary


def _row(name: str, p: PhaseSummary) -> str:
    p95 = f"{p.p95_latency_ms:.0f}" if p.p95_latency_ms is not None else "–"
    return f"| {name} | {p.attempts} | {p.successes} | {p.missing_samples} | {p95} |"


def to_markdown(r: RunReport) -> str:
    c = r.comparison
    lines = [
        f"# Run report: {r.run_id}",
        "",
        f"- **Capability:** `{r.capability_id}` {r.plan.get('parameters') or ''}",
        f"- **Outcome:** {r.state.value}" + (f" ({r.stop_reason.value})" if r.stop_reason else ""),
        f"- **Cleanup:** {r.cleanup_status}",
        f"- **Conclusive:** {'yes' if r.conclusive else 'no'}"
        + ("" if r.impact_observed is None else f"; impact observed: {'yes' if r.impact_observed else 'no'}"),
        f"- **Planned duration:** {r.plan.get('duration_seconds')} s; requested by {r.plan.get('requested_by')}",
        "",
        "## Baseline, fault and recovery",
        "",
        "| Phase | Attempts | Successful checkouts | Missing samples | p95 ms |",
        "|---|---|---|---|---|",
        _row("Baseline (pre-run)", c.baseline), _row("Fault", c.fault), _row("Recovery", c.recovery),
        "",
        f"Protected probes: {r.protected['failures']} failed of {r.protected['samples']}"
        + (f" ({', '.join(r.protected['failed_probes'])})" if r.protected["failed_probes"] else "") + ".",
        "",
    ]
    if r.baseline_reference:
        b = r.baseline_reference
        lines += [f"Reference baseline `{b['baseline_id']}`: {b['checkout_attempts']} checkouts, "
                  f"failure rate {b['checkout_failure_rate']}, p95 {b['checkout_p95_ms']} ms "
                  f"(healthy: {b['healthy']}).", ""]
    if r.injection:
        i = r.injection
        lines += ["## Injection and restoration", "",
                  f"- `{i['flag']}` set to `{i['variant']}` at {i['applied_at']}; flagd served "
                  f"`{i['served_value']}` (matches: {i['evaluation_matches']}).",
                  f"- Restored at {i['restored_at'] or 'not recorded'} ({i['restore_outcome'] or '–'}); "
                  f"flagd matches prior value: {i['restore_evaluation_matches']}. Restorer: {i['restorer'] or '–'}.",
                  ""]
    lines += ["## Timeline", "", "| Seq | Time (UTC) | State | Reason | By |", "|---|---|---|---|---|"]
    lines += [f"| {t.seq} | {t.at.isoformat()} | {t.state.value} | {t.stop_reason.value if t.stop_reason else ''} "
              f"| {t.by or ''} |" for t in r.timeline]
    lines += ["", "## Differences", ""] + [f"- {d}" for d in c.differences]
    lines += ["", "## Missing data", ""] + ([f"- {m}" for m in r.missing_data] or ["- none recorded"])
    lines += ["", "## Limitations", ""] + [f"- {m}" for m in r.limitations]
    lines += ["", f"Evidence: {r.evidence['event_count']} events, sha256 `{r.evidence['events_sha256']}`. "
              f"Report `{r.report_id}`.", ""]
    return "\n".join(lines)
