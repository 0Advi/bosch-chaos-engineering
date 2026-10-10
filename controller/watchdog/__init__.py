"""Independent restoration supervisor (P2-03).

Runs as its own process (started per run in a new session), reads only the
run store, and takes over restoration when the controller dies or a run
passes its hard deadline. It never decides what to inject.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import time
from typing import Callable, Optional

from ..catalog import load_policy
from ..executor.environment import Environment
from ..executor.recovery import drive_recovery
from ..schemas import RunState as S, StopReason as R, TERMINAL
from ..store import RunStore, pid_alive

DEFAULT_DB = Path(__file__).resolve().parents[2] / "artifacts" / "results" / "controller.sqlite3"


def _grace(run: dict, default: float) -> float:
    limits = (run.get("recovery") or {}).get("limits")
    return limits["watchdog_grace_s"] if limits else default


def takeover_reason(run: dict, now: float, grace: float) -> tuple[Optional[R], bool]:
    """(reason, steal) when the watchdog must restore; (None, False) otherwise."""
    state = S(run["state"])
    if state in TERMINAL or state == S.NEEDS_ATTENTION:
        return None, False
    if run["hard_deadline_at"] and now > run["hard_deadline_at"] + grace:
        return R.DEADLINE, True                 # controller alive but stuck past its deadline
    if not pid_alive(run["controller_pid"]):
        return R.CONTROLLER_LOST, False
    return None, False


def watch(store: RunStore, env: Environment, run_id: str, poll: float = 1.0,
          default_grace: float = 10.0, max_seconds: Optional[float] = None) -> S:
    """Supervise one run until it is terminal, needs attention, or is taken over."""
    started = time.monotonic()
    who = f"watchdog:{os.getpid()}"
    while True:
        run = store.get(run_id)
        state = S(run["state"])
        if state in TERMINAL or state == S.NEEDS_ATTENTION:
            return state
        reason, steal = takeover_reason(run, time.time(), _grace(run, default_grace))
        if reason:
            store.emit(run_id, "warning", watchdog="takeover", stop_reason=reason.value, steal=steal)
            return drive_recovery(store, env, run_id, reason, who, steal)
        if max_seconds is not None and time.monotonic() - started > max_seconds:
            return state
        time.sleep(poll)


def reconcile_all(store: RunStore, env: Environment, default_grace: float = 10.0) -> list[dict]:
    """One pass over unfinished runs, e.g. after a host or controller restart."""
    results = []
    for run in store.active_runs():
        reason, steal = takeover_reason(run, time.time(), _grace(run, default_grace))
        if reason:
            state = drive_recovery(store, env, run["run_id"], reason, f"watchdog:{os.getpid()}", steal)
            results.append({"run_id": run["run_id"], "state": state.value, "reason": reason.value})
        else:
            results.append({"run_id": run["run_id"], "state": run["state"], "reason": None})
    return results


def main(argv=None, env_factory: Optional[Callable[[], Environment]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--run-id")
    target.add_argument("--all", action="store_true", help="reconcile every unfinished run once")
    parser.add_argument("--poll", type=float, default=1.0)
    args = parser.parse_args(argv)
    if env_factory is None:
        from ..executor.environment import LiveEnvironment
        env_factory = LiveEnvironment
    store, env = RunStore(args.db), env_factory()
    grace = load_policy().get("watchdog_grace_seconds", 10.0)
    if args.all:
        for row in reconcile_all(store, env, grace):
            print(row, flush=True)
        return 0
    state = watch(store, env, args.run_id, args.poll, grace)
    print(f"{args.run_id}: {state.value}", flush=True)
    return 0 if state != S.NEEDS_ATTENTION else 2


if __name__ == "__main__":
    sys.exit(main())
