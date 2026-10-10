"""Turn workload attempts into contract Observations (P1-02).

An attempt is counted whether or not it succeeded. Missing or late telemetry
is listed in Observation.missing and makes the observation incomplete; it is
never replaced with inferred data.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

from ..schemas import Observation
from .telemetry import Telemetry

REQUIRED_SERVICES = ("frontend", "checkout", "payment")


def _outcome(http_status: Optional[int], transport_error: Optional[str]) -> str:
    if transport_error == "timeout":
        return "timeout"
    if http_status is None:
        return "not_observed"
    return "success" if http_status == 200 else "http_error"


def wait_for_trace(telemetry: Telemetry, trace_id: str, wait_seconds: float,
                   poll_seconds: float = 1.0) -> tuple[Optional[dict], Optional[float]]:
    """Poll Jaeger until the trace appears; return (summary, seconds waited)."""
    started = time.monotonic()
    while True:
        summary = telemetry.trace(trace_id)
        waited = time.monotonic() - started
        if summary is not None:
            return summary, round(waited, 2)
        if waited >= wait_seconds:
            return None, None
        time.sleep(poll_seconds)


def observe_checkout(attempt: dict, telemetry: Optional[Telemetry], run_id: str,
                     trace_wait_seconds: float = 20, check_logs: bool = True) -> tuple[Observation, dict]:
    """Correlate one checkout attempt with its trace and logs."""
    missing, detail, services, span_ids, tx_ids, rpc_code = [], {}, [], [], [], None
    trace_id = attempt.get("trace_id")
    if attempt.get("failed_step"):
        missing.append(f"checkout_not_sent:{attempt['failed_step']}")
    elif telemetry is None:
        missing.append("telemetry_not_checked")
    else:
        summary, waited = wait_for_trace(telemetry, trace_id, trace_wait_seconds)
        detail["trace_wait_seconds"] = waited
        if summary is None:
            missing.append("trace")
        else:
            detail["trace"] = summary
            services = summary["services"]
            missing += [f"span:{s}" for s in REQUIRED_SERVICES if s not in services]
            if not summary["charge_spans"]:
                missing.append("payment_charge_span")
            span_ids = [s["span_id"] for s in summary["charge_spans"]]
            tx_ids = summary["transaction_ids"]
            codes = [s["rpc_code"] for s in summary["error_spans"] if s["rpc_code"] is not None]
            rpc_code = str(codes[0]) if codes else None
        if check_logs:
            logs = telemetry.logs_for_trace(trace_id)
            detail["logs"] = logs
            if logs is None:
                missing.append("logs_unavailable")
            elif not logs["hits"]:
                missing.append("logs_for_trace")

    items = attempt.get("cart_items_after")
    observation = Observation(
        observation_id="obs-" + attempt["attempt_id"].removeprefix("att-"),
        run_id=run_id, at=datetime.fromisoformat(attempt["at"]) if attempt.get("at") else datetime.now(timezone.utc),
        probe="POST /api/checkout", phase=attempt["phase"], request_id=attempt["attempt_id"],
        trace_id=trace_id, span_ids=[s for s in span_ids if s], outcome=_outcome(
            attempt.get("http_status"), attempt.get("transport_error")),
        http_status=attempt.get("http_status"), rpc_code=rpc_code, latency_ms=attempt.get("latency_ms"),
        cart_retained=None if items is None else items > 0, transaction_ids=tx_ids,
        log_refs=[f"opensearch:otel-logs*?trace={trace_id}"] if trace_id and check_logs else [],
        services=services, expected_outcome_met=attempt.get("matched"),
        fresh="trace" in detail,
        complete=not missing, missing=missing)
    return observation, detail


def observe_probe(result: dict, run_id: str, phase: str, index: int) -> Observation:
    """Protected-path probe: the HTTP result itself is the signal."""
    return Observation(
        observation_id=f"obs-{result['trace_id'][:12]}-{index}", run_id=run_id,
        at=datetime.fromisoformat(result["at"]), probe=result["probe"], phase=phase,
        trace_id=result["trace_id"], outcome=_outcome(result["http_status"], result["transport_error"]),
        http_status=result["http_status"], latency_ms=result["latency_ms"], fresh=True, complete=True)
