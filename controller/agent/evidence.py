"""Blind evidence bundles for diagnosis (P4-03).

A bundle holds only what an operator could observe: checkout and protected
probe outcomes, run phase timing, and (optionally) correlated traces/logs.
The injection manifest, capability, flag names, adapter events and runner
messages that name the fault are removed, and a final scan fails closed if
any scenario term remains.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Optional

from ..catalog import Catalog
from ..observer.sanitize import sanitize
from ..schemas import Event

FLAG_TERMS = ("paymentFailure", "paymentUnreachable")


class BlindingError(ValueError):
    """The bundle still names the injected scenario; it must not reach the model."""


@dataclass
class EvidenceBundle:
    """Blind evidence for diagnosis: observable items with stable ids, signals that could not be
    collected, and collection notes.
    """
    items: list[dict]
    missing: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def ids(self) -> set[str]:
        """The ids of all evidence items."""
        return {i["id"] for i in self.items}

    def by_id(self, item_id: str) -> Optional[dict]:
        """The evidence item with this id, or None."""
        return next((i for i in self.items if i["id"] == item_id), None)

    def canonical(self) -> str:
        """Deterministic JSON of the bundle, used for hashing and blinding checks."""
        return json.dumps({"items": self.items, "missing": self.missing, "notes": self.notes},
                          sort_keys=True, default=str)

    @property
    def sha256(self) -> str:
        """Hash of the canonical bundle, recorded with each diagnosis."""
        return hashlib.sha256(self.canonical().encode()).hexdigest()

    def as_prompt(self) -> str:
        """Render the bundle as the tagged evidence block sent to the model."""
        lines = ["<evidence>"]
        for item in self.items:
            lines.append(json.dumps(item, sort_keys=True, default=str))
        lines.append("</evidence>")
        lines.append("<missing_signals>" + json.dumps(self.missing) + "</missing_signals>")
        if self.notes:
            lines.append("<collection_notes>" + json.dumps(self.notes) + "</collection_notes>")
        return "\n".join(lines)

    @classmethod
    def from_dict(cls, data: dict) -> "EvidenceBundle":
        """Rebuild a bundle from its saved dict form."""
        return cls(items=list(data["items"]), missing=list(data.get("missing", [])),
                   notes=list(data.get("notes", [])))


def scenario_terms() -> list[str]:
    """Words that would reveal the injected scenario (flag names and capability ids), used to keep
    evidence blind.
    """
    return sorted(set(FLAG_TERMS) | set(Catalog().by_id))


def assert_blind(bundle: EvidenceBundle) -> None:
    """Raise BlindingError if the evidence mentions any scenario term."""
    text = bundle.canonical()
    leaked = [t for t in scenario_terms() if t in text]
    if leaked:
        raise BlindingError(f"evidence names the scenario: {leaked}")


def _attempt(event: Event) -> dict:
    """The observable fields of one checkout attempt event."""
    d = event.data
    return {k: d.get(k) for k in ("phase", "http_status", "error_code", "latency_ms", "matched",
                                   "cart_items_after", "trace_id", "transport_error") if k in d}


def from_run(record: dict, events: list[Event], telemetry=None, max_traces: int = 6) -> EvidenceBundle:
    """Build a blind bundle from a run record and its events (store or exported files)."""
    items, n = [], 0

    def add(kind: str, at, data: dict) -> dict:
        """Append one sanitized evidence item with the next id and return it."""
        nonlocal n
        n += 1
        item = {"id": f"ev-{n:03d}", "kind": kind, "at": at.isoformat() if hasattr(at, "isoformat") else at,
                "data": sanitize(data)}
        items.append(item)
        return item

    traced = 0
    for event in sorted(events, key=lambda e: e.seq):
        d = event.data
        if event.type == "state_changed":
            add("run_phase", event.at, {"state": event.state.value})
        elif event.type == "observation" and d.get("probe") == "POST /api/checkout":
            item = add("checkout_attempt", event.at, _attempt(event))
            if telemetry is not None and d.get("trace_id") and traced < max_traces and d.get("phase") == "fault":
                traced += 1
                summary = telemetry.trace(d["trace_id"])
                if summary is None:
                    add("trace", event.at, {"trace_id": d["trace_id"], "found": False, "for": item["id"]})
                else:
                    add("trace", event.at, {"trace_id": d["trace_id"], "found": True, "for": item["id"],
                                            "services": summary["services"],
                                            "error_spans": summary["error_spans"],
                                            "charge_spans": summary["charge_spans"],
                                            "transaction_ids": summary["transaction_ids"]})
        elif event.type == "observation" and d.get("kind") == "protected":
            add("protected_probes", event.at, {"phase": d.get("phase"), "samples": d.get("samples"),
                                               "failed": d.get("failed", [])})
        elif event.type == "observation" and d.get("phase") == "fault" and "kind" not in d and "probe" not in d:
            add("synthetic_request", event.at, {k: v for k, v in d.items() if k not in ("fixture", "mode")})
        # Deliberately dropped: action_applied/action_restored, flag_evaluation observations,
        # warnings/errors/notes (runner messages name the adapter), and plan/capability data.
    missing = []
    if telemetry is None:
        missing.append("traces and logs were not collected for this run")
    if not any(i["kind"] == "checkout_attempt" and i["data"].get("phase") == "fault" for i in items):
        missing.append("no checkout attempts recorded during the fault window")
    bundle = EvidenceBundle(items, missing,
                            [f"run outcome recorded by the controller: {record.get('state')}"])
    assert_blind(bundle)
    return bundle
