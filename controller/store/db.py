"""Durable run metadata and append-only event journal in SQLite (P2-01).

The controller and the independent watchdog share this database. Every state
change is validated against the lifecycle and written together with its event
in one IMMEDIATE transaction, so both processes see one ordered history.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Iterator, Optional

from ..schemas import Event, Plan, RunState as S, StopReason as R, TERMINAL, check_transition

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id            TEXT PRIMARY KEY,
    plan_id           TEXT NOT NULL,
    request_id        TEXT NOT NULL UNIQUE,
    capability_id     TEXT NOT NULL,
    state             TEXT NOT NULL,
    stop_reason       TEXT,
    plan_json         TEXT NOT NULL,
    recovery_json     TEXT,
    result_json       TEXT,
    created_at        REAL NOT NULL,
    fault_deadline_at REAL,
    hard_deadline_at  REAL,
    heartbeat_at      REAL,
    controller_pid    INTEGER,
    watchdog_pid      INTEGER,
    restorer          TEXT,
    abort_requested   INTEGER NOT NULL DEFAULT 0,
    last_seq          INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    seq    INTEGER NOT NULL,
    at     TEXT NOT NULL,
    body   TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
"""
TERMINAL_VALUES = tuple(s.value for s in TERMINAL)


def owner_pid(owner: Optional[str]) -> Optional[int]:
    try:
        return int(owner.rsplit(":", 1)[1]) if owner else None
    except (IndexError, ValueError):
        return None


def pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class RunInProgress(RuntimeError):
    def __init__(self, run_id: str, state: str):
        super().__init__(f"run {run_id} is {state}; only one run may be active")
        self.run_id, self.state = run_id, state


class DuplicateRequest(RuntimeError):
    def __init__(self, request_id: str, run_id: str):
        super().__init__(f"request {request_id} already created run {run_id}")
        self.request_id, self.run_id = request_id, run_id


class RunStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    # ---------------------------------------------------------------- reads
    def get(self, run_id: str) -> dict:
        row = self.db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        run = dict(row)
        for key in ("recovery_json", "result_json", "plan_json"):
            run[key.removesuffix("_json")] = json.loads(run[key]) if run[key] else None
        return run

    def active_runs(self) -> list[dict]:
        rows = self.db.execute(
            f"SELECT run_id FROM runs WHERE state NOT IN ({','.join('?' * len(TERMINAL_VALUES))})"
            " ORDER BY created_at", TERMINAL_VALUES).fetchall()
        return [self.get(r["run_id"]) for r in rows]

    def events(self, run_id: str, after_seq: int = 0) -> list[Event]:
        rows = self.db.execute("SELECT body FROM events WHERE run_id = ? AND seq > ? ORDER BY seq",
                               (run_id, after_seq)).fetchall()
        return [Event.model_validate_json(r["body"]) for r in rows]

    # --------------------------------------------------------------- writes
    def create(self, run_id: str, plan: Plan, controller_pid: int) -> dict:
        """Insert a DRAFT run, enforcing one active run and unique request IDs."""
        with self.tx() as db:
            dup = db.execute("SELECT run_id FROM runs WHERE request_id = ?", (plan.request_id,)).fetchone()
            if dup:
                raise DuplicateRequest(plan.request_id, dup["run_id"])
            active = db.execute(
                f"SELECT run_id, state FROM runs WHERE state NOT IN ({','.join('?' * len(TERMINAL_VALUES))})",
                TERMINAL_VALUES).fetchone()
            if active:
                raise RunInProgress(active["run_id"], active["state"])
            now = time.time()
            db.execute("INSERT INTO runs (run_id, plan_id, request_id, capability_id, state, plan_json,"
                       " created_at, heartbeat_at, controller_pid) VALUES (?,?,?,?,?,?,?,?,?)",
                       (run_id, plan.plan_id, plan.request_id, plan.capability_id, S.DRAFT.value,
                        plan.model_dump_json(), now, now, controller_pid))
            self._append(db, run_id, "note", S.DRAFT, None, {"created": True, "capability": plan.capability_id})
        return self.get(run_id)

    def _append(self, db, run_id: str, type_: str, state: S, reason: Optional[R], data: dict) -> Event:
        seq = db.execute("SELECT last_seq FROM runs WHERE run_id = ?", (run_id,)).fetchone()["last_seq"] + 1
        event = Event(run_id=run_id, seq=seq, at=datetime.now(timezone.utc), type=type_,
                      state=state, stop_reason=reason, data=json.loads(json.dumps(data, default=str)))
        db.execute("INSERT INTO events (run_id, seq, at, body) VALUES (?,?,?,?)",
                   (run_id, seq, event.at.isoformat(), event.model_dump_json()))
        db.execute("UPDATE runs SET last_seq = ? WHERE run_id = ?", (seq, run_id))
        return event

    def transition(self, run_id: str, target: S, reason: Optional[R] = None, **data) -> Event:
        with self.tx() as db:
            row = db.execute("SELECT state, stop_reason FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            current = S(row["state"])
            reason = reason or (R(row["stop_reason"]) if row["stop_reason"] else None)
            check_transition(current, target, reason)
            db.execute("UPDATE runs SET state = ?, stop_reason = ? WHERE run_id = ?",
                       (target.value, reason.value if reason else None, run_id))
            return self._append(db, run_id, "state_changed", target, reason, data)

    def emit(self, run_id: str, type_: str, **data) -> Event:
        with self.tx() as db:
            row = db.execute("SELECT state, stop_reason FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            reason = R(row["stop_reason"]) if row["stop_reason"] else None
            return self._append(db, run_id, type_, S(row["state"]), reason, data)

    def update(self, run_id: str, **fields) -> None:
        allowed = {"recovery_json", "result_json", "fault_deadline_at", "hard_deadline_at",
                   "watchdog_pid", "controller_pid"}
        if not fields or set(fields) - allowed:
            raise ValueError(f"cannot update {sorted(set(fields) - allowed)}")
        values = [json.dumps(v) if k.endswith("_json") else v for k, v in fields.items()]
        with self.tx() as db:
            db.execute(f"UPDATE runs SET {', '.join(f'{k} = ?' for k in fields)} WHERE run_id = ?",
                       (*values, run_id))

    def heartbeat(self, run_id: str) -> None:
        self.db.execute("UPDATE runs SET heartbeat_at = ? WHERE run_id = ?", (time.time(), run_id))

    def request_abort(self, run_id: str) -> bool:
        with self.tx() as db:
            cur = db.execute(
                f"UPDATE runs SET abort_requested = 1 WHERE run_id = ? AND state NOT IN "
                f"({','.join('?' * len(TERMINAL_VALUES))})", (run_id, *TERMINAL_VALUES))
            if cur.rowcount:
                self._append(db, run_id, "note", S(db.execute(
                    "SELECT state FROM runs WHERE run_id = ?", (run_id,)).fetchone()["state"]),
                    None, {"abort_requested": True})
            return bool(cur.rowcount)

    def claim_restorer(self, run_id: str, who: str, steal: bool = False) -> bool:
        """Compare-and-set the single process allowed to restore this run.

        `who` is "role:pid". A claim succeeds when nobody owns restoration, the
        caller already owns it, the owner process is dead, or `steal` is set
        (hard-deadline takeover from a live but stuck controller).
        """
        with self.tx() as db:
            owner = db.execute("SELECT restorer FROM runs WHERE run_id = ?", (run_id,)).fetchone()["restorer"]
            if owner in (None, who) or steal or not pid_alive(owner_pid(owner)):
                db.execute("UPDATE runs SET restorer = ? WHERE run_id = ?", (who, run_id))
                return True
            return False

    def release_restorer(self, run_id: str) -> None:
        self.db.execute("UPDATE runs SET restorer = NULL WHERE run_id = ?", (run_id,))
