"""Durable, guarded single-run executor (P2-01).

Dry-run by default. A live run needs the capability in the execution policy,
an approved sandbox, a healthy fresh baseline and a derived payment policy.
The run store enforces one active run and unique request IDs. The prepared
change is journaled before anything is mutated, an independent watchdog is
started per run, and every stop path ends in drive_recovery().
"""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import signal
import subprocess
import time
from typing import Callable, Optional
from uuid import uuid4

from ..baseline import check as baseline_check, policy as payment_policy
from ..baseline.measure import load_criteria
from ..catalog import Catalog, Unsupported, is_live, load_policy
from ..observer import endpoints
from ..observer.sanitize import sanitize
from ..safety import sandbox
from ..schemas import AdapterKind, Capability, Plan, RunState as S, StopReason as R
from ..store import DuplicateRequest, RunInProgress, RunStore, pid_alive
from . import adapters
from .environment import Environment, LiveEnvironment, ObservationError
from .flag_store import FLAG_FILE, FlagChange, FlagConflict, baseline_problems
from .limits import RunLimits
from .recovery import drive_recovery, journal, wait_for_flag

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "artifacts" / "results"
DB_PATH = RESULTS / "controller.sqlite3"


class StopRun(Exception):
    def __init__(self, reason: R, detail: str):
        super().__init__(detail)
        self.reason, self.detail = reason, detail


class LostOwnership(Exception):
    """The watchdog took over this run; the controller must not touch it again."""


# ------------------------------------------------------------- validation
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


def load_limits(policy: dict) -> RunLimits:
    if not payment_policy.POLICY_PATH.exists():
        raise Unsupported("PAYMENT_POLICY_MISSING",
                          "config/payment_policy.yaml not found; run make baseline twice and make derive-policy")
    return RunLimits.from_policies(payment_policy.load(), policy)


def live_gates(plan: Plan, cap: Capability, policy: dict, spec: dict, env: Environment,
               root: Path = ROOT) -> list[str]:
    """Implementation and evidence gates; an empty list is required for a live run."""
    blockers = []
    if not is_live(policy, cap.id):
        blockers.append(f"{cap.id} is not listed as live in config/execution_policy.yaml")
    try:
        report = env.sandbox_report()
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        blockers.append(f"sandbox: Docker inspection unavailable ({type(error).__name__}); isolation unverified")
    else:
        if not report.isolation_approved:
            blockers += [f"sandbox: {e}" for e in report.errors] or ["sandbox: not approved"]
    ref = plan.sandbox
    if (ref.docker_context, ref.daemon_id, ref.compose_project) != (
            spec["docker_context"], spec["daemon_id"], spec["compose_project"]):
        blockers.append("plan sandbox identity differs from config/sandbox.yaml")
    if not plan.baseline_ref:
        blockers.append("plan.baseline_ref must name a healthy baseline (make baseline)")
    elif not (root / plan.baseline_ref).resolve().is_relative_to((root / "artifacts").resolve()):
        blockers.append("baseline: reference escapes the artifact directory")
    else:
        blockers += [f"baseline: {b}" for b in baseline_check.usable_blockers(
            root / plan.baseline_ref, spec["daemon_id"], env.flag_store.path,
            load_criteria()["baseline_max_age_hours"], profile=load_criteria()["workload"],
            sandbox_identity=spec, upstream_revision="dedc0178918e260823323b8d95005a8cb924b007")]
    if cap.adapter == AdapterKind.FLAG:
        blockers += [f"flags not at baseline: {p}" for p in baseline_problems(env.flag_store.path)]
    return blockers


def reconcile_orphans(store: RunStore, env: Environment) -> list[dict]:
    """Restart recovery: finish runs whose controller died (and whose watchdog is gone)."""
    handled = []
    for run in store.active_runs():
        if run["state"] == S.NEEDS_ATTENTION.value or pid_alive(run["controller_pid"]):
            continue
        state = drive_recovery(store, env, run["run_id"], R.CONTROLLER_LOST, f"controller:{os.getpid()}")
        handled.append({"run_id": run["run_id"], "state": state.value})
    return handled


# ------------------------------------------------------------------ runner
class Runner:
    def __init__(self, store: RunStore, env: Environment, limits: RunLimits,
                 log: Callable[[str], None] = print):
        self.store, self.env, self.limits, self.log = store, env, limits, log
        self.who = f"controller:{os.getpid()}"
        self.signal_reason: Optional[R] = None
        self.result = {"affected": {"samples": 0, "failures": 0},
                       "protected": {"samples": 0, "failures": 0}}

    def run(self, plan: Plan, cap: Capability, params: dict) -> dict:
        return self.drive(self.create(plan), plan, cap, params)

    def create(self, plan: Plan) -> str:
        """Record the run (DRAFT) and start its watchdog; rejects duplicates/overlap."""
        run_id = f"run-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid4().hex[:6]}"
        try:
            self.store.create(run_id, plan, os.getpid())
        except DuplicateRequest as dup:
            raise Unsupported("DUPLICATE_REQUEST", str(dup), run_id=dup.run_id)
        except RunInProgress as busy:
            raise Unsupported("RUN_IN_PROGRESS", str(busy), run_id=busy.run_id, state=busy.state)
        try:
            self._spawn_watchdog(run_id)
        except Exception:
            self.store.transition(run_id, S.ABORTED, stop_reason=R.PRECONDITION_FAILED)
            raise Unsupported("WATCHDOG_UNAVAILABLE", "recovery watchdog failed to acknowledge readiness; no fault was applied", run_id=run_id)
        return run_id

    def drive(self, run_id: str, plan: Plan, cap: Capability, params: dict) -> dict:
        """Execute a created run to a terminal or NEEDS_ATTENTION state."""
        previous = self._install_signals()
        reason = R.COMPLETED
        try:
            services = self._preflight(run_id, cap)
            change = self._arm(run_id, plan, cap, params, services)
            self._fault(run_id, plan, cap, params, change)
            self._check_owner(run_id)
        except LostOwnership:
            self._finish_result(cap)
            self.store.update(run_id, result_json=self.result)
            return self._summary(run_id, note="watchdog took over restoration")
        except StopRun as stop:
            reason = stop.reason
            self.store.emit(run_id, "warning", stop_reason=stop.reason.value, detail=stop.detail[:300])
        except KeyboardInterrupt:
            reason = R.MANUAL_ABORT
        except ObservationError as error:
            reason = R.OBSERVATION_LOSS
            self.store.emit(run_id, "error", message=str(error)[:300])
        except FlagConflict as error:
            reason = R.CONFIG_CONFLICT
            self.store.emit(run_id, "error", message=str(error)[:300])
        except Exception as error:  # any adapter failure still restores
            reason = R.ADAPTER_ERROR
            self.store.emit(run_id, "error", message=f"{type(error).__name__}: {error}"[:300])
        finally:
            self._restore_signals(previous)
        self._finish_result(cap)
        self.store.update(run_id, result_json=self.result)
        drive_recovery(self.store, self.env, run_id, reason, self.who)
        return self._summary(run_id)

    # ---------------------------------------------------------------- phases
    def _preflight(self, run_id: str, cap: Capability) -> dict:
        """Recheck sandbox, configuration and application health right before arming."""
        self.store.update(run_id, recovery_json=journal(self.limits))
        self.store.transition(run_id, S.BASELINING)
        report = self.env.sandbox_report()
        if not report.isolation_approved:
            raise StopRun(R.PRECONDITION_FAILED, f"sandbox no longer approved: {report.errors[:3]}")
        if cap.adapter == AdapterKind.FLAG and (problems := baseline_problems(self.env.flag_store.path)):
            raise StopRun(R.PRECONDITION_FAILED, f"flags not at baseline: {problems}")
        services = self.env.service_states()
        pre = self.env.checkout_probe(self.limits.checkout_timeout_s)
        self._observe(run_id, "baseline", pre)
        protected = self.env.protected_probes(self.limits.protected_timeout_s)
        if not pre.get("matched") or not all(p["ok"] for p in protected):
            raise StopRun(R.PRECONDITION_FAILED, "application unhealthy immediately before injection")
        self.store.update(run_id, recovery_json=journal(self.limits, services))
        self.store.transition(run_id, S.READY)
        return services

    def _arm(self, run_id: str, plan: Plan, cap: Capability, params: dict, services: dict) -> Optional[FlagChange]:
        change = None
        if cap.adapter == AdapterKind.FLAG:
            flag, variant = adapters.flag_variant(cap, params)
            change = self.env.flag_store.prepare(flag, variant)
        # Recovery journal is durable before any mutation.
        self.store.update(run_id, recovery_json=journal(self.limits, services, change))
        fault_deadline = time.time() + plan.duration_seconds
        self.store.update(run_id, fault_deadline_at=fault_deadline,
                          hard_deadline_at=fault_deadline + self.limits.restoration_deadline_s)
        self.store.transition(run_id, S.ARMED, duration_seconds=plan.duration_seconds,
                              flag=change.flag if change else None,
                              expected_applied_sha256=change.applied_sha256 if change else None)
        return change

    def _fault(self, run_id: str, plan: Plan, cap: Capability, params: dict,
               change: Optional[FlagChange]) -> None:
        self._check_stop(run_id, None, expect=S.ARMED)
        self.store.transition(run_id, S.RUNNING)
        deadline = time.monotonic() + plan.duration_seconds
        if change is None:
            results = self.env.run_fixture(cap, params, run_id, self.limits.checkout_timeout_s)
            for item in results:
                self.store.emit(run_id, "observation", phase="fault", **sanitize(item))
            self.result["fixture_results"] = len(results)
            return
        self.env.flag_store.apply(change)
        self.store.emit(run_id, "action_applied", flag=change.flag, variant=change.variant,
                        file_sha256=change.applied_sha256)
        ok, served = wait_for_flag(self.env, change.flag, change.value, self.limits.flag_propagation_timeout_s)
        self.store.emit(run_id, "observation", phase="fault", kind="flag_evaluation",
                        flag=change.flag, served=served, matches=ok)
        if not ok:
            raise StopRun(R.ADAPTER_ERROR, f"flagd did not serve {change.flag}={change.value!r} (got {served!r})")
        self._probe_loop(run_id, change, deadline)

    def _probe_loop(self, run_id: str, change: FlagChange, deadline: float) -> None:
        lim, last_observation = self.limits, time.monotonic()
        stale_after = lim.observation_max_age_s + lim.checkout_timeout_s + 4 * lim.protected_timeout_s
        while time.monotonic() < deadline:
            tick = time.monotonic()
            self._check_stop(run_id, change)
            attempt = self.env.checkout_probe(lim.checkout_timeout_s)
            self._count("affected", not attempt.get("matched"))
            self._observe(run_id, "fault", attempt)
            protected = self.env.protected_probes(lim.protected_timeout_s)
            for p in protected:
                self._count("protected", not p["ok"])
            self.store.emit(run_id, "observation", phase="fault", kind="protected",
                            failed=[p["probe"] for p in protected if not p["ok"]], samples=len(protected))
            if time.monotonic() - last_observation > stale_after:
                raise StopRun(R.OBSERVATION_LOSS, "observations arrived later than the policy allows")
            last_observation = time.monotonic()
            self._check_protected()
            self.store.heartbeat(run_id)
            self._sleep(run_id, change, min(tick + lim.probe_interval_s, deadline))

    # --------------------------------------------------------------- checks
    def _check_stop(self, run_id: str, change: Optional[FlagChange], expect: S = S.RUNNING) -> None:
        if self.signal_reason:
            raise StopRun(self.signal_reason, "signal received")
        run = self._check_owner(run_id, expect)
        if run["abort_requested"]:
            raise StopRun(R.MANUAL_ABORT, "abort requested")
        if change is not None and self.env.flag_store.state_of(change) != "applied":
            raise StopRun(R.CONFIG_CONFLICT, "flag file changed externally during the run")

    def _check_owner(self, run_id: str, expect: S = S.RUNNING) -> dict:
        run = self.store.get(run_id)
        if run["state"] != expect.value or run["restorer"] not in (None, self.who):
            raise LostOwnership()
        return run

    def _check_protected(self) -> None:
        p, lim = self.result["protected"], self.limits
        too_many = p["failures"] > lim.allowed_protected_failures
        rate_bad = p["samples"] >= lim.protected_min_samples and \
            p["failures"] / p["samples"] > lim.protected_max_error_rate
        if too_many or rate_bad:
            raise StopRun(R.PROTECTED_PATH_DEGRADED,
                          f"protected failures {p['failures']}/{p['samples']} exceed policy")

    def _sleep(self, run_id: str, change: FlagChange, until: float) -> None:
        while (remaining := until - time.monotonic()) > 0:
            time.sleep(min(remaining, 0.2))
            self._check_stop(run_id, change)
            self.store.heartbeat(run_id)

    # ---------------------------------------------------------------- helpers
    def _count(self, kind: str, failed: bool) -> None:
        self.result[kind]["samples"] += 1
        self.result[kind]["failures"] += int(failed)

    def _observe(self, run_id: str, phase: str, attempt: dict) -> None:
        self.store.emit(run_id, "observation", phase=phase, probe="POST /api/checkout",
                        http_status=attempt.get("http_status"), error_code=attempt.get("error_code"),
                        latency_ms=attempt.get("latency_ms"), matched=attempt.get("matched"),
                        cart_items_after=attempt.get("cart_items_after"), trace_id=attempt.get("trace_id"),
                        transport_error=attempt.get("transport_error"))

    def _finish_result(self, cap: Capability) -> None:
        a = self.result["affected"]
        if cap.adapter == AdapterKind.FLAG:
            self.result["conclusive"] = a["samples"] >= self.limits.min_affected_samples
            self.result["impact_observed"] = a["failures"] > 0
            if not self.result["conclusive"]:
                self.result["note"] = (f"only {a['samples']} checkout samples "
                                       f"(< {self.limits.min_affected_samples}); result is inconclusive")
        else:
            self.result["conclusive"] = self.result.get("fixture_results", 0) > 0

    def _summary(self, run_id: str, **extra) -> dict:
        run = self.store.get(run_id)
        return {"run_id": run_id, "state": run["state"], "stop_reason": run["stop_reason"],
                "result": run["result"], "events": run["last_seq"], **extra}

    def _spawn_watchdog(self, run_id: str) -> None:
        argv = self.env.watchdog_argv(self.store.path, run_id)
        if not argv:
            if isinstance(self.env, LiveEnvironment):
                raise RuntimeError("live execution requires an independent watchdog")
            self.store.emit(run_id, "warning", detail="no watchdog configured for this environment")
            return
        log = (self.store.path.parent / f"{run_id}.watchdog.log").open("ab")
        proc = subprocess.Popen(argv, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                start_new_session=True)   # survives the controller's process group
        log.close()
        self.store.update(run_id, watchdog_pid=proc.pid)
        self.store.emit(run_id, "note", watchdog_pid=proc.pid)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if any(e.data.get("watchdog_ready") is True and e.data.get("watchdog_pid") == proc.pid
                   for e in self.store.events(run_id)):
                return
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        if proc.poll() is None:
            proc.terminate()
        raise RuntimeError("watchdog readiness acknowledgement missing")

    def _install_signals(self):
        def handler(signum, _frame):
            self.signal_reason = R.MANUAL_ABORT
        try:
            return {s: signal.signal(s, handler) for s in (signal.SIGINT, signal.SIGTERM)}
        except ValueError:          # not the main thread (tests); abort via the store instead
            return {}

    @staticmethod
    def _restore_signals(previous: dict) -> None:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


# --------------------------------------------------------------- entrypoint
def dry_run(plan: Plan) -> dict:
    catalog, policy, spec = Catalog(), load_policy(), sandbox.load_spec()
    cap, params = validate(plan, catalog, policy)
    actions = adapters.describe(cap, params, spec["network"], endpoints.app_url())
    return {"mode": "dry_run", "capability": cap.id, "parameters": params, "actions": actions}


def prepare(plan: Plan, *, env: Optional[Environment] = None, store: Optional[RunStore] = None,
            limits: Optional[RunLimits] = None,
            gate_check: Optional[Callable[[Plan, Capability], list[str]]] = None,
            log: Callable[[str], None] = print) -> tuple[Runner, Capability, dict]:
    """Validate and gate a live plan without creating any run state."""
    catalog, policy, spec = Catalog(), load_policy(), sandbox.load_spec()
    cap, params = validate(plan, catalog, policy)
    env = env or LiveEnvironment(spec)
    blockers = gate_check(plan, cap) if gate_check else live_gates(plan, cap, policy, spec, env)
    try:
        limits = limits or load_limits(policy)
    except Unsupported as missing:
        blockers.append(missing.message)
    if limits and plan.duration_seconds > limits.fault_hard_cap_s:
        blockers.append(f"duration exceeds the {limits.fault_hard_cap_s}s hard cap")
    if blockers:
        raise Unsupported("LIVE_EXECUTION_BLOCKED", "live execution prerequisites are not met",
                          blockers=blockers)
    store = store or RunStore(DB_PATH)
    reconcile_orphans(store, env)
    return Runner(store, env, limits, log), cap, params


def execute(plan: Plan, live: bool = False, *, env: Optional[Environment] = None,
            store: Optional[RunStore] = None, limits: Optional[RunLimits] = None,
            gate_check: Optional[Callable[[Plan, Capability], list[str]]] = None,
            log: Callable[[str], None] = print, results_dir: Optional[Path] = None) -> dict:
    if not live:
        return dry_run(plan)
    run, cap, params = prepare(plan, env=env, store=store, limits=limits, gate_check=gate_check, log=log)
    summary = run.run(plan, cap, params)
    # Persist sanitized evidence and the report next to the run (P3-01).
    from ..report.evidence import export_run
    summary["evidence"] = str(export_run(run.store, summary["run_id"],
                                         results_dir or run.store.path.parent))
    return summary
