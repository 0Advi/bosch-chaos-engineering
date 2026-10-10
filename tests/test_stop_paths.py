"""P2-03 watchdog/restart recovery and P2-04 stop paths.

Every path must end with the flag file back at its exact prior bytes and a
verified recovery, or in NEEDS_ATTENTION that blocks further runs.
"""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time

import pytest

from controller import watchdog
from controller.catalog import Unsupported
from controller.executor import runner
from controller.executor.flag_store import FLAG_FILE
from controller.executor.recovery import drive_recovery
from controller.schemas import RunState as S, StopReason as R, validate_event_sequence
from controller.store import RunStore
from tests.fake_env import FakeEnvironment, fast_limits, make_plan

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def flag_file(tmp_path):
    path = tmp_path / "flagd" / "demo.flagd.json"
    path.parent.mkdir()
    shutil.copy(FLAG_FILE, path)
    return path


@pytest.fixture
def db(tmp_path):
    return tmp_path / "runs.sqlite3"


def execute(plan, env, db, **limits):
    return runner.execute(plan, live=True, env=env, store=RunStore(db), limits=fast_limits(**limits),
                          gate_check=lambda p, c: [], log=lambda line: None)


def wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    raise AssertionError("condition not reached in time")


def running(db):
    store = RunStore(db)
    runs = store.active_runs()
    return runs[0]["run_id"] if runs and runs[0]["state"] == "RUNNING" and any(
        e.type == "action_applied" for e in store.events(runs[0]["run_id"])) else None


def assert_restored(flag_file):
    assert flag_file.read_bytes() == FLAG_FILE.read_bytes()


# ----------------------------------------------------------- stop paths
def test_manual_abort_from_another_process_restores(flag_file, db):
    def abort_when_running():
        wait_for(lambda: running(db))
        RunStore(db).request_abort(running(db))
    threading.Thread(target=abort_when_running, daemon=True).start()
    result = execute(make_plan(duration=20), FakeEnvironment(flag_file), db)
    assert (result["state"], result["stop_reason"]) == ("ABORTED", "manual_abort")
    assert_restored(flag_file)


def test_protected_path_degradation_aborts(flag_file, db):
    result = execute(make_plan(duration=20), FakeEnvironment(flag_file, protected_fail_when_faulted=True), db)
    assert (result["state"], result["stop_reason"]) == ("ABORTED", "protected_path_degraded")
    assert result["result"]["protected"]["failures"] > fast_limits().allowed_protected_failures
    assert_restored(flag_file)


def test_observer_loss_aborts(flag_file, db):
    result = execute(make_plan(duration=20), FakeEnvironment(flag_file, observer_fail_after=2), db)
    assert (result["state"], result["stop_reason"]) == ("ABORTED", "observation_loss")
    assert_restored(flag_file)


def test_concurrent_edit_is_not_overwritten_and_blocks_new_runs(flag_file, db):
    def edit_when_running():
        wait_for(lambda: running(db))
        doc = json.loads(flag_file.read_text())
        doc["flags"]["cartFailure"]["defaultVariant"] = "10%"
        flag_file.write_text(json.dumps(doc, indent=2))
    threading.Thread(target=edit_when_running, daemon=True).start()
    result = execute(make_plan(duration=20), FakeEnvironment(flag_file), db)
    assert (result["state"], result["stop_reason"]) == ("NEEDS_ATTENTION", "config_conflict")
    assert json.loads(flag_file.read_text())["flags"]["cartFailure"]["defaultVariant"] == "10%"
    with pytest.raises(Unsupported) as blocked:
        execute(make_plan(), FakeEnvironment(flag_file), db)
    assert blocked.value.code == "RUN_IN_PROGRESS"
    # Operator puts the file back by hand, then retries restoration explicitly.
    shutil.copy(FLAG_FILE, flag_file)
    state = drive_recovery(RunStore(db), FakeEnvironment(flag_file), result["run_id"], None, "operator:1")
    assert state == S.ABORTED
    assert validate_event_sequence(RunStore(db).events(result["run_id"])) == S.ABORTED


def test_too_few_samples_is_inconclusive_not_success(flag_file, db):
    result = execute(make_plan(duration=0), FakeEnvironment(flag_file), db, min_affected_samples=50)
    assert result["state"] == "RECOVERED"
    assert result["result"]["conclusive"] is False and "inconclusive" in result["result"]["note"]


def test_unrelated_service_change_needs_attention(flag_file, db):
    result = execute(make_plan(), FakeEnvironment(flag_file, restart_service_after_fault=True), db)
    assert result["state"] == "NEEDS_ATTENTION"
    events = RunStore(db).events(result["run_id"])
    assert any("services changed" in json.dumps(e.data) for e in events)
    assert_restored(flag_file)


def test_failed_recovery_is_not_reported_as_recovered(flag_file, db):
    result = execute(make_plan(), FakeEnvironment(flag_file, recovery_fails=True), db)
    assert result["state"] == "NEEDS_ATTENTION"
    assert_restored(flag_file)


def test_only_the_selected_flag_changes_during_a_run(flag_file, db):
    seen = []

    def snapshot_when_running():
        wait_for(lambda: running(db))
        seen.append(json.loads(flag_file.read_text())["flags"])
        RunStore(db).request_abort(running(db))
    threading.Thread(target=snapshot_when_running, daemon=True).start()
    execute(make_plan(duration=20), FakeEnvironment(flag_file), db)
    before = json.loads(FLAG_FILE.read_text())["flags"]
    changed = [k for k in before if before[k] != seen[0][k]]
    assert changed == ["paymentUnreachable"]


# --------------------------------------------------- watchdog (P2-03)
def test_watchdog_takes_over_a_stuck_controller_at_the_hard_deadline(flag_file, db):
    """Controller hangs inside a probe; the watchdog restores after deadline + grace."""
    env = FakeEnvironment(flag_file, probe_hang_s=4)
    outcome = {}

    def supervise():
        rid = None
        wait_for(lambda: running(db))
        rid = running(db)
        outcome["state"] = watchdog.watch(RunStore(db), env, rid, poll=0.1, max_seconds=15)
    thread = threading.Thread(target=supervise, daemon=True)
    thread.start()
    result = execute(make_plan(duration=1), env, db, restoration_deadline_s=0.5, watchdog_grace_s=0.5)
    thread.join(20)
    assert outcome["state"] == S.ABORTED
    assert (result["state"], result["stop_reason"]) == ("ABORTED", "deadline")
    assert result.get("note") == "watchdog took over restoration"
    assert_restored(flag_file)


def _spawn_controller(tmp_path, flag_file, db, watchdog_enabled):
    env = FakeEnvironment(flag_file, watchdog=watchdog_enabled)
    config = env.save(tmp_path / "env.json")
    plan = tmp_path / "plan.json"
    plan.write_text(make_plan(duration=25).model_dump_json())
    return subprocess.Popen([sys.executable, "tests/helpers/run_fake.py", "--config", str(config),
                             "--db", str(db), "--plan", str(plan)], cwd=ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def test_hard_kill_of_controller_is_restored_by_detached_watchdog(tmp_path, flag_file, db):
    proc = _spawn_controller(tmp_path, flag_file, db, watchdog_enabled=True)
    try:
        wait_for(lambda: running(db), timeout=20)
        assert flag_file.read_bytes() != FLAG_FILE.read_bytes()          # fault really applied
        os.kill(proc.pid, signal.SIGKILL)
    finally:
        proc.wait(10)                                                   # reap so the PID is gone
    store = RunStore(db)
    run_id = store.db.execute("SELECT run_id FROM runs").fetchone()["run_id"]
    wait_for(lambda: store.get(run_id)["state"] in ("ABORTED", "NEEDS_ATTENTION"), timeout=20)
    run = store.get(run_id)
    assert (run["state"], run["stop_reason"]) == ("ABORTED", "controller_lost")
    assert run["restorer"].startswith("watchdog:")
    assert validate_event_sequence(store.events(run_id)) == S.ABORTED
    assert_restored(flag_file)


def test_restart_reconciliation_restores_when_no_watchdog_survived(tmp_path, flag_file, db):
    proc = _spawn_controller(tmp_path, flag_file, db, watchdog_enabled=False)
    try:
        wait_for(lambda: running(db), timeout=20)
        os.kill(proc.pid, signal.SIGKILL)
    finally:
        proc.wait(10)
    store = RunStore(db)
    run_id = store.active_runs()[0]["run_id"]
    assert store.get(run_id)["state"] == "RUNNING"                     # stuck until reconciled
    results = watchdog.reconcile_all(store, FakeEnvironment(flag_file))
    assert results == [{"run_id": run_id, "state": "ABORTED", "reason": "controller_lost"}]
    assert_restored(flag_file)


@pytest.mark.parametrize("run, expected", [
    ({"state": "RUNNING", "hard_deadline_at": 100.0, "controller_pid": os.getpid()}, (None, False)),
    ({"state": "RUNNING", "hard_deadline_at": 10.0, "controller_pid": os.getpid()}, (R.DEADLINE, True)),
    ({"state": "ARMED", "hard_deadline_at": 100.0, "controller_pid": 999999}, (R.CONTROLLER_LOST, False)),
    ({"state": "NEEDS_ATTENTION", "hard_deadline_at": 10.0, "controller_pid": 999999}, (None, False)),
    ({"state": "RECOVERED", "hard_deadline_at": 10.0, "controller_pid": 999999}, (None, False)),
])
def test_takeover_rules(run, expected):
    assert watchdog.takeover_reason(run, now=50.0, grace=5.0) == expected
