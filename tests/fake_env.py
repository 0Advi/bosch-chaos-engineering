"""Fake sandbox for runner/watchdog tests (no Docker).

It derives application behavior from the real flag file, like flagd would:
checkout fails while paymentUnreachable is on or paymentFailure > 0. Options
simulate the stop paths P2-04 must handle.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import threading
import time

from controller.agent.tools import ToolRegistry
from controller.executor.environment import ObservationError
from controller.executor.flag_store import FlagStore
from controller.executor.limits import RunLimits
from controller.safety.sandbox import ScopeReport
from controller.schemas import Plan

PROTECTED = ("GET /", "GET /api/products", "POST /api/cart", "GET /api/cart")


def make_plan(capability: str = "payment-unreachable", duration: int = 1, **params) -> Plan:
    """A valid plan for a capability, built through the real propose_plan tool."""
    out = ToolRegistry().dispatch("propose_plan", {
        "capability_id": capability, "parameters": params, "duration_seconds": duration,
        "expected_impact": "checkout fails; protected paths stay healthy"})
    assert out["ok"], out
    return Plan.model_validate(out["result"])


def fast_limits(**overrides) -> RunLimits:
    """Run limits shrunk to fractions of a second so runner tests finish quickly."""
    values = dict(policy_version="test", checkout_timeout_s=1, protected_timeout_s=1, probe_interval_s=0.1,
                  min_affected_samples=3, protected_min_samples=8, protected_max_error_rate=0.25,
                  observation_max_age_s=2, restoration_deadline_s=1, recovery_checks=2, fault_hard_cap_s=30,
                  flag_propagation_timeout_s=1, watchdog_grace_s=0.5, watchdog_poll_s=0.1)
    values.update(overrides)
    return RunLimits(**values)


class FakeEnvironment:
    """In-memory stand-in for the sandbox: reads the real flag file, simulates probes and can be
    told to misbehave.
    """
    OPTIONS = ("propagate", "protected_fail_when_faulted", "observer_fail_after", "probe_hang_s",
               "restart_service_after_fault", "unhealthy_before", "recovery_fails", "watchdog")

    def __init__(self, flag_file: Path, *, propagate=True, protected_fail_when_faulted=False,
                 observer_fail_after=None, probe_hang_s=0.0, restart_service_after_fault=False,
                 unhealthy_before=False, recovery_fails=False, watchdog=False, config_path=None):
        """Configure the fake: flag propagation, failing protected paths or observer, hanging
        probes, restarts, failing recovery and an optional watchdog.
        """
        self.flag_store = FlagStore(Path(flag_file))
        self.propagate, self.protected_fail = propagate, protected_fail_when_faulted
        self.observer_fail_after, self.probe_hang_s = observer_fail_after, probe_hang_s
        self.restart_after, self.unhealthy_before = restart_service_after_fault, unhealthy_before
        self.recovery_fails, self.watchdog, self.config_path = recovery_fails, watchdog, config_path
        self.initial = {f: self._value(f) for f in ("paymentUnreachable", "paymentFailure")}
        self.fault_seen, self.faulted_checkouts = False, 0
        self.lock = threading.Lock()

    # ------------------------------------------------------------ helpers
    def _value(self, flag):
        """The value a flag currently serves according to the flag file."""
        entry = json.loads(self.flag_store.path.read_text())["flags"][flag]
        return entry["variants"][entry["defaultVariant"]]

    def faulted(self) -> bool:
        """Whether a payment fault flag is active."""
        return bool(self._value("paymentUnreachable")) or self._value("paymentFailure") > 0

    # ------------------------------------------------------- environment API
    def evaluate_flag(self, flag):
        """The flag value flagd would serve (stuck at the initial value when propagation is off)."""
        return self._value(flag) if self.propagate else self.initial[flag]

    def checkout_probe(self, timeout):
        """A simulated checkout: fails while faulted, can hang, and can lose observability."""
        faulted = self.faulted()
        if faulted:
            with self.lock:
                self.fault_seen = True
                self.faulted_checkouts += 1
                n = self.faulted_checkouts
            if self.observer_fail_after is not None and n > self.observer_fail_after:
                raise ObservationError("telemetry backend unreachable")
            if self.probe_hang_s:
                time.sleep(self.probe_hang_s)
        ok = not faulted and not self.unhealthy_before and not (self.recovery_fails and self.fault_seen)
        return {"matched": ok, "http_status": 200 if ok else 500, "latency_ms": 5.0,
                "trace_id": "0" * 32, "error_code": None, "transport_error": None}

    def protected_probes(self, timeout):
        """Simulated protected-path probes (fail while faulted if configured)."""
        ok = not (self.protected_fail and self.faulted())
        return [{"probe": p, "ok": ok, "http_status": 200 if ok else 503, "latency_ms": 2.0} for p in PROTECTED]

    def sandbox_report(self):
        """An approved scope report."""
        return ScopeReport(approved=True)

    def service_states(self):
        """Payment and checkout container states, with a restart if configured."""
        restarted = 1 if self.restart_after and self.fault_seen else 0
        return {"payment": {"id": "pay1", "status": "running", "restarts": restarted},
                "checkout": {"id": "chk1", "status": "running", "restarts": 0}}

    def run_fixture(self, cap, params, run_id, timeout):
        """A successful fixture result."""
        return [{"fixture": cap.id, "http_status": 200, "order_id": "order-1"}]

    def watchdog_argv(self, db_path, run_id):
        """Command for the fake watchdog process, or None if disabled."""
        if not self.watchdog:
            return None
        return [sys.executable, "tests/helpers/watchdog_fake.py", "--db", str(db_path),
                "--run-id", run_id, "--poll", "0.1", "--config", str(self.config_path)]

    # ------------------------------------------------------- subprocess use
    def save(self, path: Path) -> Path:
        """Save the fake's options to a file so a separate process can rebuild it."""
        self.config_path = path
        options = {k: getattr(self, {"protected_fail_when_faulted": "protected_fail",
                                     "restart_service_after_fault": "restart_after"}.get(k, k))
                   for k in self.OPTIONS}
        path.write_text(json.dumps({"flag_file": str(self.flag_store.path), **options}))
        return path

    @classmethod
    def load(cls, path: Path) -> "FakeEnvironment":
        """Rebuild a fake environment from a saved options file."""
        data = json.loads(Path(path).read_text())
        return cls(data.pop("flag_file"), config_path=path, **data)
