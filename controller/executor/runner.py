"""Preview runner and deferred P2 execution scaffold.

Live execution is unconditionally blocked until P2. The draft gates below
check for a healthy fresh baseline (P1), but enforced hard deadlines, an
independent watchdog and recovery testing are still needed before P2 enables it.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

from ..baseline import check as baseline_check
from ..baseline.measure import load_criteria
from ..catalog import Catalog, Unsupported, is_live, load_policy
from ..observer import endpoints
from ..safety import sandbox
from ..schemas import AdapterKind, Event, Plan, RunState as S, StopReason as R, check_transition
from . import adapters
from .flag_store import FLAG_FILE, FlagStore, baseline_problems

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "artifacts" / "results"
LOCK = RESULTS / ".run.lock"


class Journal:
    """Append-only, fsynced JSONL event log with contiguous sequence numbers."""

    def __init__(self, run_id: str):
        self.run_id, self.seq, self.state, self.reason = run_id, 0, S.DRAFT, None
        self.dir = RESULTS / run_id
        self.dir.mkdir(parents=True, exist_ok=False)
        self.path = self.dir / "events.jsonl"

    def emit(self, type_: str, data: dict | None = None) -> None:
        self.seq += 1
        event = Event(run_id=self.run_id, seq=self.seq, at=datetime.now(timezone.utc),
                      type=type_, state=self.state, stop_reason=self.reason, data=data or {})
        with self.path.open("a") as handle:
            handle.write(event.model_dump_json() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        print(f"[{self.seq:03d}] {self.state.value:<19} {type_} {json.dumps(data or {})[:160]}")

    def to(self, state: S, reason: R | None = None, **data) -> None:
        self.reason = reason or self.reason
        check_transition(self.state, state, self.reason)
        self.state = state
        self.emit("state_changed", data)


@contextmanager
def run_lock():
    RESULTS.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise Unsupported("RUN_IN_PROGRESS", f"another run holds {LOCK.name}; "
                          "check artifacts/results/ for NEEDS_ATTENTION before removing it")
    hold = {"keep": False}  # set True when cleanup is unverified: block new runs
    try:
        os.write(fd, str(os.getpid()).encode())
        yield hold
    finally:
        os.close(fd)
        if not hold["keep"]:
            LOCK.unlink(missing_ok=True)


def validate(plan: Plan, catalog: Catalog, policy: dict) -> tuple:
    cap = catalog.plannable(plan.capability_id)
    try:
        params = cap.check_parameters(plan.parameters)
    except ValueError as error:
        raise Unsupported("PARAMETER_OUT_OF_BOUNDS", str(error))
    limit = min(cap.duration.max_seconds, policy["max_duration_seconds"])
    if plan.duration_seconds > limit:
        raise Unsupported("DURATION_TOO_LONG", f"duration {plan.duration_seconds}s exceeds {limit}s")
    if plan.policy_version != policy["policy_version"]:
        raise Unsupported("STALE_POLICY", f"plan uses {plan.policy_version}, current is {policy['policy_version']}")
    return cap, params


def live_gates(plan: Plan, cap, policy: dict, spec: dict) -> list[str]:
    # This is an implementation gate, not an operator-configurable switch.
    # Merely changing approved/execution_enabled must never activate P2 drafts.
    return ["P0 supports preview only; P1 baseline and P2 safety/watchdog are not implemented"]


def _draft_live_gates(plan: Plan, cap, policy: dict, spec: dict) -> list[str]:
    """Deferred P2 checks; deliberately not used to authorize execution."""
    blockers = []
    if not is_live(policy, cap.id):
        blockers.append(f"{cap.id} is not listed as live in config/execution_policy.yaml")
    report = sandbox.evaluate(sandbox.collect_snapshot(), spec)
    if not report.isolation_approved:
        blockers += [f"sandbox: {e}" for e in report.errors] or ["sandbox: not approved"]
    ref = plan.sandbox
    if (ref.docker_context, ref.daemon_id, ref.compose_project) != (
            spec["docker_context"], spec["daemon_id"], spec["compose_project"]):
        blockers.append("plan sandbox identity differs from config/sandbox.yaml")
    if not plan.baseline_ref:
        blockers.append("plan.baseline_ref must name a healthy baseline (make baseline)")
    else:
        blockers += [f"baseline: {b}" for b in baseline_check.usable_blockers(
            ROOT / plan.baseline_ref, spec["daemon_id"], FLAG_FILE,
            load_criteria()["baseline_max_age_hours"])]
    if cap.adapter == AdapterKind.FLAG:
        blockers += [f"flags not at baseline: {p}" for p in baseline_problems()]
    return blockers


def execute(plan: Plan, live: bool = False) -> dict:
    catalog, policy, spec = Catalog(), load_policy(), sandbox.load_spec()
    cap, params = validate(plan, catalog, policy)
    actions = adapters.describe(cap, params, spec["network"], endpoints.app_url())
    if not live:
        return {"mode": "dry_run", "capability": cap.id, "parameters": params, "actions": actions}
    if blockers := live_gates(plan, cap, policy, spec):
        raise Unsupported("LIVE_EXECUTION_BLOCKED", "live execution prerequisites are not met",
                          blockers=blockers)

    with run_lock() as hold:
        run_id = f"run-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{plan.plan_id}"[:64]
        journal = Journal(run_id)
        (journal.dir / "plan.json").write_text(plan.model_dump_json(indent=2))
        shop = adapters.Shop(endpoints.app_url(), policy["request_timeout_seconds"])
        store, prior, applied, results = FlagStore(), None, None, []

        journal.to(S.BASELINING)
        pre = adapters.valid_checkout(shop)
        journal.emit("observation", {"phase": "baseline", **pre})
        if not pre["ok"]:
            journal.to(S.FAILED, R.ADAPTER_ERROR, reason="pre-run valid checkout failed")
            return {"run_id": run_id, "state": journal.state.value}
        journal.to(S.READY)

        if cap.adapter == AdapterKind.FLAG:
            flag, _ = adapters.flag_variant(cap, params)
            prior = store.snapshot(flag)
            # Persist the recovery target before mutating anything.
            (journal.dir / "recovery.json").write_text(json.dumps(prior.__dict__, default=str))
        journal.to(S.ARMED, actions=actions)

        reason = R.COMPLETED
        try:
            journal.to(S.RUNNING)
            if cap.adapter == AdapterKind.FLAG:
                prior, applied = adapters.flag_apply(store, cap, params)
                journal.emit("action_applied", {"flag": applied.flag, "variant": applied.default_variant})
                deadline = time.monotonic() + plan.duration_seconds
                while time.monotonic() < deadline:
                    probe = adapters.valid_checkout(shop)
                    journal.emit("observation", {"phase": "fault", **probe})
                    time.sleep(min(3, max(0, deadline - time.monotonic())))
            elif cap.id == "checkout-currency-mismatch":
                results = adapters.currency_mismatch(shop, cap, params)
            elif cap.id == "checkout-duplicate-submit":
                results = adapters.duplicate_submit(shop, cap, params)
            elif cap.adapter == AdapterKind.PAYMENT_GRPC:
                results = adapters.payment_charge(cap, params, spec["network"], run_id,
                                                  policy["request_timeout_seconds"])
            for item in results:
                journal.emit("observation", {"phase": "fault", **item})
        except KeyboardInterrupt:
            reason = R.MANUAL_ABORT
        except Exception as error:  # restore regardless, record why
            reason = R.CONFIG_CONFLICT if "changed externally" in str(error) else R.ADAPTER_ERROR
            journal.emit("error", {"message": str(error)[:300]})
        finally:
            if journal.state in (S.ARMED, S.RUNNING):
                journal.to(S.RESTORING, reason)
            try:
                if prior and applied:
                    restored = store.restore(prior, applied.sha256)
                    journal.emit("action_restored", {"flag": restored.flag, "variant": restored.default_variant})
            except Exception as error:
                journal.emit("error", {"message": f"restore failed: {error}"[:300]})
                journal.to(S.NEEDS_ATTENTION)
                hold["keep"] = True
                return {"run_id": run_id, "state": journal.state.value, "lock": "kept"}

        journal.to(S.VERIFYING_RECOVERY)
        checks = [adapters.valid_checkout(shop) for _ in range(policy["recovery_checks"])]
        for check in checks:
            journal.emit("observation", {"phase": "recovery", **check})
        if not all(c["ok"] for c in checks):
            journal.to(S.NEEDS_ATTENTION, recovered=False)
            hold["keep"] = True
            return {"run_id": run_id, "state": journal.state.value, "lock": "kept"}
        final = {R.COMPLETED: S.RECOVERED, R.ADAPTER_ERROR: S.FAILED}.get(reason, S.ABORTED)
        journal.to(final, recovered=True)
    return {"run_id": run_id, "state": journal.state.value, "events": str(journal.path)}
