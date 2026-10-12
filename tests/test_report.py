"""P3-01: evidence export and reports reproducible from saved evidence only."""
from datetime import datetime, timezone
import hashlib
import json
import shutil

import pytest

from controller.executor import runner
from controller.executor.flag_store import FLAG_FILE
from controller.report.build import build_report
from controller.report.evidence import export_run, report_from_dir, verify_manifest
from controller.report.render import to_markdown
from controller.schemas import Baseline, ProbeStats, RunReport, SandboxRef
from controller.store import RunStore
from tests.fake_env import FakeEnvironment, fast_limits, make_plan


@pytest.fixture
def flag_file(tmp_path):
    """Fixture: a temp copy of the committed flag file."""
    path = tmp_path / "flagd" / "demo.flagd.json"
    path.parent.mkdir()
    shutil.copy(FLAG_FILE, path)
    return path


def run(tmp_path, flag_file, plan=None, **env_options):
    """Execute a fake live run and return its store and summary."""
    store = RunStore(tmp_path / "results" / "runs.sqlite3")
    summary = runner.execute(plan or make_plan(), live=True, env=FakeEnvironment(flag_file, **env_options),
                             store=store, limits=fast_limits(), gate_check=lambda p, c: [],
                             log=lambda line: None, results_dir=tmp_path / "results")
    return store, summary


def test_completed_run_exports_evidence_and_report(tmp_path, flag_file):
    """A completed run exports run.json, events, report JSON and markdown."""
    store, summary = run(tmp_path, flag_file)
    out = tmp_path / "results" / summary["run_id"]
    assert summary["evidence"] == str(out)
    assert {p.name for p in out.iterdir()} >= {"run.json", "events.jsonl", "report.json", "report.md", "manifest.json"}
    report = RunReport.model_validate_json((out / "report.json").read_text())
    assert report.state.value == "RECOVERED" and report.cleanup_status == "verified" and report.conclusive
    c = report.comparison
    assert c.baseline.attempts == 1 and c.baseline.successes == 1
    assert c.fault.attempts >= 3 and c.fault.successes == 0                 # checkout fails under fault
    assert c.recovery.attempts == 2 and c.recovery.successes == 2 and c.recovery.missing_samples == 0
    assert report.injection["flag"] == "paymentUnreachable" and report.injection["restore_outcome"] == "restored"
    assert [t.state.value for t in report.timeline][-3:] == ["RESTORING", "VERIFYING_RECOVERY", "RECOVERED"]
    assert report.evidence["event_count"] == summary["events"]


def test_report_is_reproducible_from_files_without_the_store(tmp_path, flag_file):
    """The report can be rebuilt from the exported files alone and matches."""
    store, summary = run(tmp_path, flag_file)
    out = tmp_path / "results" / summary["run_id"]
    from_db = build_report(store.get(summary["run_id"]), store.events(summary["run_id"]))
    from_files = report_from_dir(out)
    assert from_files == from_db
    assert (out / "report.json").read_text() == from_files.model_dump_json(indent=2) + "\n"
    first = {p.name: p.read_bytes() for p in out.iterdir()}
    export_run(store, summary["run_id"], tmp_path / "results")             # re-export: byte-identical
    assert {p.name: p.read_bytes() for p in out.iterdir()} == first
    assert verify_manifest(out) == []


def test_tampered_evidence_is_detected(tmp_path, flag_file):
    """Changing an exported event is detected through the digest."""
    _, summary = run(tmp_path, flag_file)
    out = tmp_path / "results" / summary["run_id"]
    lines = (out / "events.jsonl").read_text().splitlines()
    (out / "events.jsonl").write_text("\n".join(lines[:-1]) + "\n")
    assert verify_manifest(out) == ["events.jsonl"]
    assert report_from_dir(out).evidence["events_sha256"] != json.loads(
        (out / "report.json").read_text())["evidence"]["events_sha256"]


def test_stop_reasons_and_cleanup_status_are_reported(tmp_path, flag_file):
    """Stop reasons and cleanup status appear in the report."""
    _, aborted = run(tmp_path / "a", flag_file, protected_fail_when_faulted=True, plan=make_plan(duration=20))
    report = report_from_dir(tmp_path / "a" / "results" / aborted["run_id"])
    assert (report.state.value, report.stop_reason.value) == ("ABORTED", "protected_path_degraded")
    assert report.cleanup_status == "verified" and report.protected["failures"] > 0

    _, attention = run(tmp_path / "b", flag_file, restart_service_after_fault=True)
    assert report_from_dir(tmp_path / "b" / "results" / attention["run_id"]).cleanup_status == "needs_attention"

    _, never_armed = run(tmp_path / "c", flag_file, unhealthy_before=True)
    report = report_from_dir(tmp_path / "c" / "results" / never_armed["run_id"])
    assert report.cleanup_status == "not_required" and report.injection is None


def test_missing_samples_and_baseline_are_explicit(tmp_path, flag_file):
    """Missing samples and a missing baseline are stated in the report."""
    store = RunStore(tmp_path / "results" / "runs.sqlite3")
    summary = runner.execute(make_plan(duration=0), live=True, env=FakeEnvironment(flag_file), store=store,
                             limits=fast_limits(min_affected_samples=50), gate_check=lambda p, c: [],
                             log=lambda line: None, results_dir=tmp_path / "results")
    report = report_from_dir(tmp_path / "results" / summary["run_id"])
    assert not report.conclusive
    assert any("short of the policy minimum" in m for m in report.missing_data)
    assert "plan has no baseline_ref" in report.missing_data
    md = to_markdown(report)
    assert "## Missing data" in md and "short of the policy minimum" in md


def test_baseline_reference_is_linked_by_hash(tmp_path, flag_file):
    """The report links the plan's baseline by its hash."""
    now = datetime.now(timezone.utc)
    stats = [ProbeStats(probe=p, attempts=30, failures=0, failure_rate=0, p95_ms=2800.0, p99_ms=3000.0)
             for p in ("POST /api/checkout", "GET /", "GET /api/products", "POST /api/cart", "GET /api/cart")]
    baseline = Baseline(baseline_id="baseline-ref", started_at=now, finished_at=now,
                        sandbox=SandboxRef(docker_context="default", daemon_id="", compose_project="x"),
                        upstream_revision="x", flag_sha256="0" * 64, workload={}, probes=stats, controls=[],
                        telemetry={}, resources={}, health={}, criteria={}, healthy=True, blocking=[],
                        observations_file="x", limitations=[])
    path = tmp_path / "artifacts" / "baselines" / "baseline-ref.json"
    path.parent.mkdir(parents=True)
    path.write_text(baseline.model_dump_json())
    plan = make_plan().model_copy(update={"baseline_ref": "artifacts/baselines/baseline-ref.json"})
    store, summary = run(tmp_path, flag_file, plan=plan)
    report = build_report(store.get(summary["run_id"]), store.events(summary["run_id"]), root=tmp_path)
    ref = report.baseline_reference
    assert ref["baseline_id"] == "baseline-ref" and ref["checkout_p95_ms"] == 2800.0
    assert ref["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_export_refuses_unsanitizable_fixture_data(tmp_path, flag_file):
    """Export refuses events with data that cannot be sanitized (like an address)."""
    store, summary = run(tmp_path, flag_file)
    store.emit(summary["run_id"], "note", operator_note="shipped to 1600 Amphitheatre Parkway")
    with pytest.raises(ValueError, match="sanitization failed"):
        export_run(store, summary["run_id"], tmp_path / "results")
    store.emit(summary["run_id"], "note", operator_note="card 4432-8015-6152-0454 used")
    # Card-number shapes are redacted rather than refused; the address above still blocks export.
    with pytest.raises(ValueError):
        export_run(store, summary["run_id"], tmp_path / "results")


def test_run_json_omits_raw_flag_file_contents(tmp_path, flag_file):
    """run.json leaves out the raw flag-file text."""
    _, summary = run(tmp_path, flag_file)
    record = json.loads((tmp_path / "results" / summary["run_id"] / "run.json").read_text())
    change = record["recovery"]["change"]
    assert "prior_text" not in change and change["prior_sha256"] and change["applied_sha256"]
