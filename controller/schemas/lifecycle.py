"""Run state machine and resumable event-sequence rules (P0-02).

DRAFT -> BASELINING -> READY -> ARMED -> RUNNING -> RESTORING
      -> VERIFYING_RECOVERY -> RECOVERED | ABORTED | FAILED
Cleanup that cannot be confirmed ends in NEEDS_ATTENTION, which blocks new runs.
"""
from __future__ import annotations

from typing import Iterable, Optional

from .contracts import Event, RunState as S, StopReason as R

TRANSITIONS: dict[S, frozenset[S]] = {
    S.DRAFT: frozenset({S.BASELINING, S.ABORTED}),
    S.BASELINING: frozenset({S.READY, S.ABORTED, S.FAILED}),
    S.READY: frozenset({S.ARMED, S.ABORTED}),
    # ARMED means the recovery journal is persisted; any stop must restore.
    S.ARMED: frozenset({S.RUNNING, S.RESTORING}),
    S.RUNNING: frozenset({S.RESTORING}),
    S.RESTORING: frozenset({S.VERIFYING_RECOVERY, S.NEEDS_ATTENTION}),
    S.VERIFYING_RECOVERY: frozenset({S.RECOVERED, S.ABORTED, S.FAILED, S.NEEDS_ATTENTION}),
    # Only an operator-triggered restore retry may leave NEEDS_ATTENTION.
    S.NEEDS_ATTENTION: frozenset({S.RESTORING}),
    S.RECOVERED: frozenset(),
    S.ABORTED: frozenset(),
    S.FAILED: frozenset(),
}

TERMINAL = frozenset(s for s, nxt in TRANSITIONS.items() if not nxt)
BLOCKING = frozenset({S.ARMED, S.RUNNING, S.RESTORING, S.VERIFYING_RECOVERY, S.NEEDS_ATTENTION})

_ABORT_REASONS = frozenset({R.MANUAL_ABORT, R.DEADLINE, R.PROTECTED_PATH_DEGRADED,
                            R.OBSERVATION_LOSS, R.CONFIG_CONFLICT})


class IllegalTransition(ValueError):
    pass


def check_transition(current: S, target: S, stop_reason: Optional[R] = None) -> None:
    if target not in TRANSITIONS[current]:
        raise IllegalTransition(f"{current.value} -> {target.value} is not allowed")
    if current == S.VERIFYING_RECOVERY:
        expected = {S.RECOVERED: {R.COMPLETED}, S.ABORTED: _ABORT_REASONS,
                    S.FAILED: {R.ADAPTER_ERROR}}.get(target)
        if expected is not None and stop_reason not in expected:
            raise IllegalTransition(
                f"{target.value} requires stop_reason in {sorted(r.value for r in expected)}")
    if target == S.RESTORING and current in (S.ARMED, S.RUNNING) and stop_reason is None:
        raise IllegalTransition("entering RESTORING requires a stop_reason")


def validate_event_sequence(events: Iterable[Event], after_seq: int = 0,
                            start_state: S = S.DRAFT) -> S:
    """Check a (possibly resumed) event stream; return the resulting state.

    Sequence numbers are contiguous per run starting at after_seq + 1, so a
    client that reconnects with ?after_seq=N can detect gaps and replays.
    """
    state, expected_seq, run_id, reason = start_state, after_seq + 1, None, None
    for event in events:
        run_id = run_id or event.run_id
        if event.run_id != run_id:
            raise ValueError(f"event {event.seq} belongs to another run")
        if event.seq != expected_seq:
            raise ValueError(f"expected seq {expected_seq}, got {event.seq}")
        reason = event.stop_reason or reason
        if event.type == "state_changed":
            check_transition(state, event.state, reason)
            state = event.state
        elif event.state != state:
            raise ValueError(f"event {event.seq} reports {event.state.value} but run is {state.value}")
        expected_seq += 1
    return state
