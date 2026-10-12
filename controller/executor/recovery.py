"""Restoration and recovery verification shared by runner and watchdog (P2-02/P2-03).

drive_recovery() can resume from any non-terminal state using only what the
run store persisted, so a fresh process (watchdog, restarted controller)
finishes exactly what the original controller would have.
"""
from __future__ import annotations

from dataclasses import asdict
import time
from typing import Optional

from ..schemas import IllegalTransition, RunState as S, StopReason as R, TERMINAL
from ..store import RunStore
from .environment import Environment, ObservationError
from .flag_store import FlagChange, FlagConflict
from .limits import RunLimits

PRE_MUTATION = (S.DRAFT, S.BASELINING, S.READY)


def wait_for_flag(env: Environment, flag: str, expected, timeout: float, poll: float = 0.5) -> tuple[bool, object]:
    """Poll flagd until it serves `expected` (verifies the file change took effect)."""
    deadline, last = time.monotonic() + timeout, None
    while True:
        last = env.evaluate_flag(flag)
        if last == expected:
            return True, last
        if time.monotonic() >= deadline:
            return False, last
        time.sleep(poll)


def journal(limits: RunLimits, services_before: Optional[dict] = None,
            change: Optional[FlagChange] = None) -> dict:
    """The recovery journal stored before any mutation: limits, service states before the run and
    the exact flag change.
    """
    return {"limits": asdict(limits), "services_before": services_before,
            "change": change.as_journal() if change else None}


def _limits(run: dict) -> RunLimits:
    """The run's limits from its recovery journal."""
    return RunLimits(**run["recovery"]["limits"])


def restore(store: RunStore, env: Environment, run_id: str) -> None:
    """Undo the journaled change and confirm flagd serves the prior value again."""
    run = store.get(run_id)
    data = (run["recovery"] or {}).get("change")
    if not data:
        store.emit(run_id, "note", restoration="nothing_to_restore")
        return
    change, limits = FlagChange.from_journal(data), _limits(run)
    outcome = env.flag_store.undo(change)
    ok, served = wait_for_flag(env, change.flag, change.prior_value, limits.flag_propagation_timeout_s)
    store.emit(run_id, "action_restored", flag=change.flag, variant=change.prior_variant,
               file=outcome, evaluated=served, evaluation_matches=ok)
    if not ok:
        raise ObservationError(f"flagd still serves {change.flag}={served!r} after restoration")


def verify_recovery(store: RunStore, env: Environment, run_id: str) -> list[str]:
    """Problems preventing a verified recovery (empty = recovered)."""
    run, problems = store.get(run_id), []
    limits = _limits(run)
    for i in range(limits.recovery_checks):
        store.heartbeat(run_id)
        attempt = env.checkout_probe(limits.checkout_timeout_s)
        store.emit(run_id, "observation", phase="recovery", probe="POST /api/checkout",
                   http_status=attempt.get("http_status"), latency_ms=attempt.get("latency_ms"),
                   matched=attempt.get("matched"), trace_id=attempt.get("trace_id"))
        if not attempt.get("matched"):
            problems.append(f"recovery checkout {i + 1} did not succeed (HTTP {attempt.get('http_status')})")
    failed = [p["probe"] for p in env.protected_probes(limits.protected_timeout_s) if not p["ok"]]
    if failed:
        problems.append(f"protected probes failing after restoration: {failed}")
    before = run["recovery"].get("services_before")
    if before is not None:
        after = env.service_states()
        changed = sorted(s for s in set(before) | set(after) if before.get(s) != after.get(s))
        if changed:
            problems.append(f"services changed during the run: {changed}")
    return problems


def _final(reason: R) -> S:
    """The terminal state for a stop reason: RECOVERED for a completed run, FAILED for an adapter
    error, otherwise ABORTED.
    """
    return {R.COMPLETED: S.RECOVERED, R.ADAPTER_ERROR: S.FAILED}.get(reason, S.ABORTED)


def drive_recovery(store: RunStore, env: Environment, run_id: str, reason: Optional[R], who: str,
                   steal: bool = False) -> S:
    """Move a run from wherever it stopped to a terminal or NEEDS_ATTENTION state."""
    if not store.claim_restorer(run_id, who, steal):
        return S(store.get(run_id)["state"])          # another live process owns restoration
    try:
        return _drive(store, env, run_id, reason, who)
    except IllegalTransition:
        # Another restorer advanced the run concurrently (deadline takeover); keep its result.
        return S(store.get(run_id)["state"])


def _drive(store: RunStore, env: Environment, run_id: str, reason: Optional[R], who: str) -> S:
    """Move a run from any non-terminal state to a terminal one: restore the flag, verify recovery,
    or mark NEEDS_ATTENTION if cleanup cannot be confirmed.
    """
    run = store.get(run_id)
    if S(run["state"]) in TERMINAL:
        return S(run["state"])
    state = S(run["state"])
    reason = reason or (R(run["stop_reason"]) if run["stop_reason"] else R.CONTROLLER_LOST)
    if state in PRE_MUTATION:
        target = S.FAILED if state == S.BASELINING and reason == R.ADAPTER_ERROR else S.ABORTED
        store.transition(run_id, target, reason, note="stopped before any mutation", by=who)
        return target
    if state in (S.ARMED, S.RUNNING, S.NEEDS_ATTENTION):
        store.transition(run_id, S.RESTORING, reason, by=who)
        state = S.RESTORING
    if state == S.RESTORING:
        try:
            restore(store, env, run_id)
        except (FlagConflict, ObservationError, OSError) as error:
            store.transition(run_id, S.NEEDS_ATTENTION, error=str(error)[:300], by=who)
            store.release_restorer(run_id)
            return S.NEEDS_ATTENTION
        store.transition(run_id, S.VERIFYING_RECOVERY, by=who)
    try:
        problems = verify_recovery(store, env, run_id)
    except ObservationError as error:
        problems = [f"recovery could not be observed: {error}"]
    if problems:
        store.transition(run_id, S.NEEDS_ATTENTION, problems=problems, by=who)
        store.release_restorer(run_id)
        return S.NEEDS_ATTENTION
    final_reason = R(store.get(run_id)["stop_reason"])
    store.transition(run_id, _final(final_reason), final_reason, recovered=True, by=who)
    return _final(final_reason)
