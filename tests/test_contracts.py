"""P0-02: shared contracts, illegal transitions and resumable event sequences."""
import json
from pathlib import Path

from pydantic import ValidationError
import pytest

from controller.schemas import (Diagnosis, Event, IllegalTransition, Patch, Plan,
                                RunState as S, StopReason as R, check_transition,
                                validate_event_sequence)
from controller.schemas import export
from controller.schemas import contracts

FIX = Path(__file__).parent / "fixtures" / "contracts"
VALID_PLAN = json.loads((FIX / "plan.valid.json").read_text())
EVENTS = [Event.model_validate_json(line) for line in (FIX / "events.valid.jsonl").read_text().splitlines()]


def test_valid_fixtures_parse():
    Plan.model_validate(VALID_PLAN)
    Diagnosis.model_validate_json((FIX / "diagnosis.valid.json").read_text())


@pytest.mark.parametrize("name", export.MODELS)
def test_all_shared_record_fixtures_roundtrip(name):
    records = json.loads((FIX / "records.valid.json").read_text())
    model = getattr(contracts, name)
    record = model.model_validate(records[name])
    assert model.model_validate_json(record.model_dump_json()) == record


@pytest.mark.parametrize("case", json.loads((FIX / "plans.invalid.json").read_text()),
                         ids=lambda c: c["why"])
def test_malformed_plans_rejected(case):
    with pytest.raises(ValidationError):
        Plan.model_validate({**VALID_PLAN, **case["patch"]})


def test_full_event_sequence_ends_recovered():
    assert validate_event_sequence(EVENTS) == S.RECOVERED


def test_resume_after_sequence_number():
    assert validate_event_sequence(EVENTS[4:], after_seq=4, start_state=S.ARMED) == S.RECOVERED


def test_gap_and_replay_detected():
    with pytest.raises(ValueError, match="expected seq"):
        validate_event_sequence(EVENTS[:3] + EVENTS[4:])
    with pytest.raises(ValueError, match="expected seq"):
        validate_event_sequence(EVENTS[:3] + EVENTS[2:])


@pytest.mark.parametrize("current, target, reason", [
    (S.DRAFT, S.RUNNING, None),            # skip baseline/arming
    (S.RUNNING, S.RECOVERED, R.COMPLETED),  # skip restoration
    (S.RUNNING, S.RESTORING, None),        # restoring without a reason
    (S.VERIFYING_RECOVERY, S.RECOVERED, R.MANUAL_ABORT),
    (S.VERIFYING_RECOVERY, S.ABORTED, R.COMPLETED),
    (S.RECOVERED, S.RUNNING, None),        # terminal
    (S.NEEDS_ATTENTION, S.RECOVERED, R.COMPLETED),
])
def test_illegal_transitions(current, target, reason):
    with pytest.raises(IllegalTransition):
        check_transition(current, target, reason)


def test_uncited_diagnosis_rejected():
    data = json.loads((FIX / "diagnosis.valid.json").read_text())
    with pytest.raises(ValidationError, match="must cite"):
        Diagnosis.model_validate({**data, "cited_observations": []})
    Diagnosis.model_validate({**data, "cited_observations": [], "category": "insufficient_evidence",
                              "uncertainty": "high"})


@pytest.mark.parametrize("path", ["../etc/passwd", "/abs/file.go", "deploy/compose.sandbox.yaml"])
def test_patch_paths_confined(path):
    with pytest.raises(ValidationError):
        Patch(patch_id="patch-1", base_revision="a" * 40, diff_sha256="b" * 64,
              affected_files=[path], rationale="x")


def test_patch_approval_bound_to_exact_diff():
    patch = Patch(patch_id="patch-1", base_revision="a" * 40, diff_sha256="b" * 64,
                  affected_files=["src/checkout/main.go"], rationale="x", approved_diff_sha256="c" * 64)
    assert not patch.approved


def test_committed_json_schemas_are_current():
    assert export.main(check=True) == 0


@pytest.mark.parametrize("value", [True, "15", 1.0])
def test_duration_requires_an_integer_without_coercion(value):
    with pytest.raises(ValidationError):
        Plan.model_validate({**VALID_PLAN, "duration_seconds": value})


@pytest.mark.parametrize("value", ["/etc/passwd", "../baseline.json", "artifacts/../README.md", "README.md"])
def test_baseline_reference_is_confined_to_artifacts(value):
    with pytest.raises(ValidationError):
        Plan.model_validate({**VALID_PLAN, "baseline_ref": value})
