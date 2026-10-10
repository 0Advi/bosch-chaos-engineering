"""Deterministic rule-based diagnoser: the no-model baseline for evaluations (P4-04).

It applies config/payment_semantics.yaml signals literally. It is a reference
point for what simple rules achieve, not a claimed diagnostic capability.
"""
from __future__ import annotations

from ..schemas import DiagnosisResult
from .diagnose import ground
from .evidence import EvidenceBundle

CARD_TERMS = ("credit card info is invalid", "cannot process", "expired on", "only visa")


def _proposal(category, cited, service="", edge="", uncertainty="medium", next_action="", requested=(),
              alternatives=()):
    return {"category": category, "service": service, "edge": edge, "affected_paths": ["POST /api/checkout"],
            "cited_evidence": list(cited), "alternatives": list(alternatives), "uncertainty": uncertainty,
            "next_action": next_action, "requested_observations": list(requested), "suggestions": []}


def _propose(bundle: EvidenceBundle) -> dict:
    attempts = [i for i in bundle.items if i["kind"] == "checkout_attempt"]
    fault = [i for i in attempts if i["data"].get("phase") == "fault"] or attempts
    failed = [i for i in fault if i["data"].get("matched") is False]
    traces = [i for i in bundle.items if i["kind"] == "trace" and i["data"].get("found")]
    synthetic = [i for i in bundle.items if i["kind"] == "synthetic_request"]

    for item in synthetic:
        d = item["data"]
        if d.get("duplicate_accepted") or len(d.get("accepted_orders") or []) > 1:
            return _proposal("duplicate_payment_risk", [item["id"]], "checkout", "checkout->payment",
                             next_action="add an idempotency key per logical order before any retry logic")
        amount = (d.get("request") or {}).get("amount") or {}
        units, nanos = int(amount.get("units", 1) or 0), int(amount.get("nanos", 0) or 0)
        invalid = units <= 0 or not -999999999 <= nanos <= 999999999 or (units > 0 > nanos) or \
            amount.get("currencyCode") not in (None, "USD", "EUR", "CAD", "JPY", "GBP", "TRY")
        if amount and invalid and d.get("transaction_id"):
            return _proposal("amount_validation", [item["id"]], "payment", "checkout->payment",
                             next_action="validate Money invariants and currency in payment Charge")

    if not failed:
        if fault and all(i["data"].get("matched") for i in fault):
            ok_protected = all(not p["data"].get("failed") for p in bundle.items if p["kind"] == "protected_probes")
            if ok_protected:
                return _proposal("healthy", [i["id"] for i in fault[:3]], uncertainty="low" if not bundle.missing
                                 else "medium", next_action="no action")
        return _proposal("insufficient_evidence", [], uncertainty="high",
                         requested=["checkout attempts during the fault window"])

    for trace in traces:
        d = trace["data"]
        spans = d.get("error_spans", [])
        messages = " ".join(str(s.get("status_message", "")) for s in spans).lower()
        services = {s.get("service") for s in spans}
        cited = [trace["id"], d.get("for")] if d.get("for") else [trace["id"]]
        if d.get("transaction_ids") and services & {"shipping", "email"}:
            svc = "shipping" if "shipping" in services else "email"
            return _proposal("post_payment_failure", cited, svc, f"checkout->{svc}",
                             next_action="reconcile charged orders that failed after payment")
        if "currency" in services:
            return _proposal("currency_conversion", cited, "currency", "checkout->currency",
                             next_action="validate the requested currency before checkout")
        if "invalid token" in messages:
            return _proposal("handler_failure", cited, "payment", "checkout->payment",
                             next_action="inspect the payment Charge handler error")
        if any(t in messages for t in CARD_TERMS):
            return _proposal("card_decline", cited, "payment", "checkout->payment",
                             next_action="no infrastructure action; card was rejected")
        if "unavailable" in messages or "badaddress" in messages or any(
                str(s.get("rpc_code")) == "14" for s in spans):
            return _proposal("connectivity", cited, "checkout", "checkout->payment",
                             next_action="check the payment address/connectivity used by checkout")
    return _proposal("insufficient_evidence", [], uncertainty="high",
                     requested=["traces for the failing checkouts", "payment and checkout error logs"],
                     alternatives=["card_decline", "handler_failure", "connectivity"])


def diagnose_with_rules(run_id: str, bundle: EvidenceBundle) -> DiagnosisResult:
    return ground(run_id, _propose(bundle), bundle, "baseline")
