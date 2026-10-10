"""P4-04: case integrity, blind harness, grading and criteria reporting."""
import json

import pytest

from controller.agent.diagnose import CATEGORIES
from controller.agent.evidence import EvidenceBundle, assert_blind
from controller.agent.providers import FakeProvider
from evals import run as ev

DEV, HELDOUT = ev.load_cases("dev"), ev.load_cases("heldout")


@pytest.mark.parametrize("case", DEV + HELDOUT, ids=lambda c: c["case_id"])
def test_cases_are_well_formed_and_blind(case):
    assert case["split"] in case["case_id"] or case["case_id"].startswith(("dev-", "ho-"))
    if case["kind"] == "action":
        assert isinstance(case["expected"]["refuse"], bool) and case["request"]
        return
    bundle = EvidenceBundle.from_dict(case["bundle"])
    assert_blind(bundle)
    exp = case["expected"]
    assert exp["category"] in CATEGORIES and set(exp["accept_categories"]) <= set(CATEGORIES)
    assert set(exp["key_evidence"]) <= bundle.ids()
    assert exp["category"] == "insufficient_evidence" or exp["key_evidence"]


def test_splits_are_disjoint_and_cover_the_plan():
    assert not {c["case_id"] for c in DEV} & {c["case_id"] for c in HELDOUT}
    tags = {t for c in HELDOUT for t in c["tags"]}
    for required in ("healthy", "handler_failure", "connectivity", "card_decline", "insufficient_evidence",
                     "missing_telemetry", "prompt_injection", "post_payment_failure", "unsupported",
                     "currency_amount_mismatch", "duplicate_payment", "false_positive_trap"):
        assert required in tags, required


def test_baseline_heldout_run_meets_criteria_and_pins_versions():
    report = ev.evaluate("heldout", "baseline", log=lambda line: None)
    assert report["case_set_sha256"] == ev.case_set_sha(HELDOUT)
    assert not [r for r in report["criteria"] if r["pass"] is False]
    assert report["summary"]["actions"]["executed_mutations"] is None          # not measured, not "passed"
    assert any("Rule baseline" in l for l in report["limitations"])


def test_model_harness_hides_expected_answers_and_reports_failures():
    def responder(request):
        if request.tools:                                      # action case: refuse everything
            return "That is not supported here."
        return {"json": {"category": "healthy", "service": "", "edge": "", "affected_paths": [],
                         "cited_evidence": ["ev-001"], "alternatives": [], "uncertainty": "medium",
                         "next_action": "none", "requested_observations": [], "suggestions": []}}
    fake = FakeProvider(responder=responder, model="claude-opus-5-5")
    report = ev.evaluate("heldout", "model", fake, log=lambda line: None)
    seen = json.dumps([r.messages for r in fake.requests], default=str) + "".join(r.system for r in fake.requests)
    for case in HELDOUT:
        assert case["description"] not in seen or case["kind"] == "action"
    for leak in ("key_evidence", "accept_categories", "must_request_observations", "\"expected\""):
        assert leak not in seen, leak
    s = report["summary"]
    assert s["diagnosis"]["category_accuracy"] < 0.3 and s["diagnosis"]["false_positive_rate"] == 0
    assert s["actions"]["unsupported_refusal_rate"] == 1.0 and s["actions"]["supported_not_refused_rate"] == 0.0
    failed = {r["metric"] for r in report["criteria"] if r["pass"] is False}
    assert {"diagnosis.category_accuracy", "actions.supported_not_refused_rate"} <= failed
    assert s["cost"]["mean_cost_usd_per_case"] is not None and report["prompt_version"].startswith("diagnosis-v1-")
    md = ev.to_markdown(report)
    assert "**no**" in md and "Limitations" in md
