"""The runner's only contact with the sandbox (P2-01/P2-02).

The runner and watchdog depend on this interface; LiveEnvironment talks to
Docker and the loopback shop, tests substitute a fake with the same methods.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from typing import Protocol

from ..baseline.runtime import DockerRuntime
from ..observer import endpoints
from ..observer.fixtures import protected_probes, run_attempt
from ..observer.http import Shop
from ..safety import sandbox
from ..schemas import AdapterKind, Capability
from . import adapters
from .flag_store import FlagStore

ROOT = Path(__file__).resolve().parents[2]
CURL_IMAGE = "curlimages/curl:8.10.1"


class ObservationError(RuntimeError):
    """An observation could not be taken at all (as opposed to a failed request)."""


class Environment(Protocol):
    flag_store: FlagStore

    def evaluate_flag(self, flag: str) -> object: ...
    def checkout_probe(self, timeout: float) -> dict: ...
    def protected_probes(self, timeout: float) -> list[dict]: ...
    def sandbox_report(self) -> sandbox.ScopeReport: ...
    def service_states(self) -> dict: ...
    def run_fixture(self, cap: Capability, params: dict, run_id: str, timeout: float) -> list[dict]: ...
    def watchdog_argv(self, db_path: Path, run_id: str) -> list[str] | None: ...


class LiveEnvironment:
    def __init__(self, spec: dict | None = None, flag_store: FlagStore | None = None):
        self.spec = spec or sandbox.load_spec()
        self.flag_store = flag_store or FlagStore()
        self.runtime = DockerRuntime(self.spec["compose_project"])

    def evaluate_flag(self, flag: str) -> object:
        """Ask flagd itself (OFREP, inside the sandbox network) what it serves now."""
        try:
            out = subprocess.run(
                ["docker", "run", "--rm", "--network", self.spec["network"],
                 "--label", "bosch.chaos.tool=ofrep", CURL_IMAGE, "-fsS", "--max-time", "5",
                 "-X", "POST", "-H", "Content-Type: application/json", "-d", '{"context":{}}',
                 f"http://flagd:8016/ofrep/v1/evaluate/flags/{flag}"],
                capture_output=True, text=True, timeout=30, check=True).stdout
            return json.loads(out)["value"]
        except (OSError, subprocess.SubprocessError, ValueError, KeyError) as error:
            raise ObservationError(f"flag evaluation unavailable: {error}") from error

    def checkout_probe(self, timeout: float) -> dict:
        return run_attempt(Shop(endpoints.app_url(), timeout), "valid", "fault")

    def protected_probes(self, timeout: float) -> list[dict]:
        return protected_probes(Shop(endpoints.app_url(), timeout), timeout)

    def sandbox_report(self) -> sandbox.ScopeReport:
        return sandbox.evaluate(sandbox.collect_snapshot(), self.spec)

    def service_states(self) -> dict:
        try:
            return {s: {"id": v["id"], "status": v["status"], "restarts": v["restarts"]}
                    for s, v in self.runtime.inventory().items()}
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            raise ObservationError(f"service inventory unavailable: {error}") from error

    def run_fixture(self, cap: Capability, params: dict, run_id: str, timeout: float) -> list[dict]:
        shop = Shop(endpoints.app_url(), timeout)
        if cap.id == "checkout-currency-mismatch":
            return adapters.currency_mismatch(shop, cap, params)
        if cap.id == "checkout-duplicate-submit":
            return adapters.duplicate_submit(shop, cap, params)
        if cap.adapter == AdapterKind.PAYMENT_GRPC:
            return adapters.payment_charge(cap, params, self.spec["network"], run_id, timeout)
        raise ValueError(f"no fixture adapter for {cap.id}")

    def watchdog_argv(self, db_path: Path, run_id: str) -> list[str]:
        return [sys.executable, "-m", "controller.watchdog", "--db", str(db_path), "--run-id", run_id]
