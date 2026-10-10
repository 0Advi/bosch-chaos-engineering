"""Run limits from the derived payment policy plus execution policy (P2-01)."""
from __future__ import annotations

from dataclasses import dataclass

from ..schemas import PaymentPolicy


@dataclass(frozen=True)
class RunLimits:
    policy_version: str
    checkout_timeout_s: float
    protected_timeout_s: float
    probe_interval_s: float
    min_affected_samples: int
    protected_min_samples: int
    protected_max_error_rate: float
    observation_max_age_s: float
    restoration_deadline_s: float
    recovery_checks: int
    fault_hard_cap_s: float
    flag_propagation_timeout_s: float
    watchdog_grace_s: float
    watchdog_poll_s: float = 1.0

    @classmethod
    def from_policies(cls, payment: PaymentPolicy, execution: dict) -> "RunLimits":
        g = {name: gate.value for name, gate in payment.gates.items()}
        return cls(
            policy_version=payment.policy_version,
            checkout_timeout_s=g["checkout_probe_timeout_seconds"],
            protected_timeout_s=g["protected_probe_timeout_seconds"],
            probe_interval_s=g["probe_interval_seconds"],
            min_affected_samples=int(g["min_affected_samples"]),
            protected_min_samples=int(g["protected_min_samples"]),
            protected_max_error_rate=g["protected_max_error_rate"],
            observation_max_age_s=g["observation_max_age_seconds"],
            restoration_deadline_s=g["restoration_deadline_seconds"],
            recovery_checks=int(g["recovery_checks"]),
            fault_hard_cap_s=min(g["fault_hard_cap_seconds"], execution["max_duration_seconds"]),
            flag_propagation_timeout_s=execution["flag_propagation_timeout_seconds"],
            watchdog_grace_s=execution["watchdog_grace_seconds"])

    @property
    def allowed_protected_failures(self) -> int:
        """Failures tolerated before the minimum protected sample count is reached."""
        return int(self.protected_max_error_rate * self.protected_min_samples)
