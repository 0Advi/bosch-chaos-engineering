"""Versioned shared contracts (P0-02).

Controller, UI and agent all validate against these models. JSON Schema files
under schemas/ are generated from them (python -m controller.schemas.export).
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import PurePosixPath
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator

SCHEMA_VERSION = "1.0.0"

Identifier = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
GitRevision = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
ParamValue = Union[StrictBool, StrictInt, StrictStr]


class Contract(BaseModel):
    """Strict base: unknown fields are rejected, never silently ignored."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1.0.0"] = SCHEMA_VERSION


# --------------------------------------------------------------- capability
class Family(str, Enum):
    PAYMENT_UNAVAILABLE = "payment_unavailable"
    CURRENCY_AMOUNT_MISMATCH = "currency_amount_mismatch"
    DUPLICATE_PAYMENT = "duplicate_payment"


class AdapterKind(str, Enum):
    FLAG = "flag"                                  # sandbox-owned flagd file
    CHECKOUT_REQUEST = "checkout_request_fixture"  # HTTP via frontend-proxy
    PAYMENT_GRPC = "payment_grpc_fixture"          # direct Charge via grpcurl
    CONTAINER_STOP = "container_stop"              # stop/start one container


class Execution(str, Enum):
    DISABLED = "disabled"  # contract only; adapter not trusted yet
    PREVIEW = "preview"    # dry-run actions; live only if policy lists it


class ParamSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
    type: Literal["int", "enum", "bool"]
    description: str
    default: ParamValue
    minimum: Optional[int] = None
    maximum: Optional[int] = None
    choices: Optional[list[str]] = None

    @model_validator(mode="after")
    def _consistent(self):
        if self.type == "int" and (self.minimum is None or self.maximum is None):
            raise ValueError(f"int parameter {self.name} needs minimum and maximum")
        if self.type == "enum" and not self.choices:
            raise ValueError(f"enum parameter {self.name} needs choices")
        self.check(self.default)
        return self

    def check(self, value: ParamValue) -> ParamValue:
        """Validate one value; raises ValueError with a readable reason."""
        if self.type == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"{self.name} must be a boolean")
        elif self.type == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{self.name} must be an integer")
            if not self.minimum <= value <= self.maximum:
                raise ValueError(f"{self.name}={value} outside [{self.minimum}, {self.maximum}]")
        elif value not in (self.choices or []):
            raise ValueError(f"{self.name}={value!r} not in {self.choices}")
        return value


class DurationLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    default_seconds: Annotated[int, Field(ge=0, le=30, strict=True)]
    max_seconds: Annotated[int, Field(ge=0, le=30, strict=True)]


class Capability(Contract):
    id: Identifier
    title: str
    family: Family
    adapter: AdapterKind
    adapter_version: str
    execution: Execution
    disabled_reason: Optional[str] = None
    target_service: str
    affected_dependency: Optional[str] = None
    parameters: list[ParamSpec] = []
    adapter_config: dict = Field(
        default_factory=dict,
        description="Fixed operator-owned settings; never supplied by a plan or model.")
    prerequisites: list[str]
    duration: DurationLimits
    observations: list[str]
    affected_probes: list[str]
    protected_probes: list[str]
    expected_evidence: list[str]
    restoration: str
    success_criteria: list[str]

    @model_validator(mode="after")
    def _disabled_needs_reason(self):
        if self.execution == Execution.DISABLED and not self.disabled_reason:
            raise ValueError(f"{self.id}: disabled capability needs disabled_reason")
        if self.duration.default_seconds > self.duration.max_seconds:
            raise ValueError(f"{self.id}: default duration exceeds max")
        return self

    def check_parameters(self, supplied: dict[str, ParamValue]) -> dict[str, ParamValue]:
        specs = {p.name: p for p in self.parameters}
        unknown = sorted(set(supplied) - set(specs))
        if unknown:
            raise ValueError(f"unknown parameters for {self.id}: {unknown}")
        return {name: spec.check(supplied.get(name, spec.default)) for name, spec in specs.items()}


# --------------------------------------------------------------------- plan
class SandboxRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    docker_context: str
    daemon_id: str
    compose_project: str


class Plan(Contract):
    plan_id: Identifier
    request_id: Identifier
    capability_id: Identifier
    parameters: dict[str, ParamValue] = {}
    sandbox: SandboxRef
    duration_seconds: Annotated[int, Field(ge=0, le=30, strict=True)]
    baseline_ref: Optional[str] = None
    expected_impact: Annotated[str, Field(min_length=1, max_length=500)]
    policy_version: str
    requested_by: Literal["user", "agent"]

    @field_validator("baseline_ref")
    @classmethod
    def _baseline_path(cls, value):
        if value is not None:
            path = PurePosixPath(value)
            if (path.is_absolute() or ".." in path.parts or "\\" in value
                    or not value.startswith("artifacts/")):
                raise ValueError("baseline_ref must be a relative path inside artifacts/")
        return value


# ---------------------------------------------------------------------- run
class RunState(str, Enum):
    DRAFT = "DRAFT"
    BASELINING = "BASELINING"
    READY = "READY"
    ARMED = "ARMED"
    RUNNING = "RUNNING"
    RESTORING = "RESTORING"
    VERIFYING_RECOVERY = "VERIFYING_RECOVERY"
    RECOVERED = "RECOVERED"
    ABORTED = "ABORTED"
    FAILED = "FAILED"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"


class StopReason(str, Enum):
    COMPLETED = "completed"
    MANUAL_ABORT = "manual_abort"
    DEADLINE = "deadline"
    PROTECTED_PATH_DEGRADED = "protected_path_degraded"
    OBSERVATION_LOSS = "observation_loss"
    CONFIG_CONFLICT = "config_conflict"
    ADAPTER_ERROR = "adapter_error"
    CONTROLLER_LOST = "controller_lost"
    PRECONDITION_FAILED = "precondition_failed"


class Revisions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source: str
    images: dict[str, str] = {}
    config_sha256: Optional[Sha256] = None
    workload_version: str


class Run(Contract):
    run_id: Identifier
    plan_id: Identifier
    state: RunState
    stop_reason: Optional[StopReason] = None
    revisions: Revisions
    prior_state: dict = {}
    applied_state: dict = {}
    restored_state: dict = {}
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    last_event_seq: Annotated[int, Field(ge=0)] = 0
    evidence_refs: list[str] = []
    cleanup_verified: bool = False
    recovery_outcome: Optional[Literal["verified", "unverified", "failed"]] = None


class Event(Contract):
    run_id: Identifier
    seq: Annotated[int, Field(ge=1)]
    at: datetime
    type: Literal["state_changed", "action_applied", "action_restored",
                  "observation", "warning", "error", "note"]
    state: RunState
    stop_reason: Optional[StopReason] = None
    data: dict = {}


# -------------------------------------------------------------- observation
class Observation(Contract):
    observation_id: Identifier
    run_id: Identifier
    at: datetime
    probe: str
    phase: Literal["warmup", "baseline", "fault", "recovery", "control"]
    request_id: Optional[str] = None
    trace_id: Optional[Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]] = None
    span_ids: list[str] = []
    outcome: Literal["success", "http_error", "rpc_error", "timeout", "not_observed"]
    http_status: Optional[int] = None
    rpc_code: Optional[str] = None
    latency_ms: Optional[Annotated[float, Field(ge=0)]] = None
    cart_retained: Optional[bool] = None
    transaction_ids: list[str] = []
    metric_refs: list[str] = []
    log_refs: list[str] = []
    services: list[str] = []
    expected_outcome_met: Optional[bool] = None
    fresh: bool
    complete: bool
    missing: list[str] = Field(
        default_factory=list,
        description="Signals that were expected but not observed; never filled in by inference.")

    @model_validator(mode="after")
    def _honest_completeness(self):
        if self.complete and self.missing:
            raise ValueError("an observation with missing signals cannot be complete")
        return self


# ----------------------------------------------------------------- baseline
class ProbeStats(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    probe: str
    attempts: Annotated[int, Field(ge=0)]
    failures: Annotated[int, Field(ge=0)]
    failure_rate: Annotated[float, Field(ge=0, le=1)]
    p50_ms: Optional[float] = None
    p90_ms: Optional[float] = None
    p95_ms: Optional[float] = None
    p99_ms: Optional[float] = None
    max_ms: Optional[float] = None


class Baseline(Contract):
    baseline_id: Identifier
    started_at: datetime
    finished_at: datetime
    sandbox: SandboxRef
    upstream_revision: str
    flag_sha256: Sha256
    workload: dict
    probes: list[ProbeStats]
    controls: list[dict]
    telemetry: dict
    resources: dict
    health: dict
    criteria: dict
    healthy: bool
    blocking: list[str]
    observations_file: str
    limitations: list[str]

    @model_validator(mode="after")
    def _verdict_matches_blockers(self):
        if self.healthy == bool(self.blocking):
            raise ValueError("healthy must be true exactly when there are no blocking findings")
        return self

    def probe(self, name: str) -> ProbeStats:
        return next(p for p in self.probes if p.probe == name)


class Gate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    value: Union[int, float]
    unit: str
    rule: str
    inputs: dict


class PaymentPolicy(Contract):
    policy_version: str
    derived_at: datetime
    derived_from: Annotated[list[dict], Field(min_length=2)]
    affected_probes: list[str]
    protected_probes: list[str]
    gates: dict[str, Gate]
    limitations: list[str]


# ---------------------------------------------------------------- diagnosis
class DiagnosisCategory(str, Enum):
    HEALTHY = "healthy"
    CARD_DECLINE = "card_decline"
    HANDLER_FAILURE = "handler_failure"
    CONNECTIVITY = "connectivity"
    CURRENCY_CONVERSION = "currency_conversion"
    AMOUNT_VALIDATION = "amount_validation"
    DUPLICATE_PAYMENT_RISK = "duplicate_payment_risk"
    UNKNOWN_OUTCOME = "unknown_outcome"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class Diagnosis(Contract):
    run_id: Identifier
    service: Optional[str] = None
    edge: Optional[Annotated[str, Field(pattern=r"^[a-z-]+->[a-z-]+$")]] = None
    category: DiagnosisCategory
    affected_paths: list[str] = []
    cited_observations: list[Identifier] = []
    alternatives: list[str] = []
    uncertainty: Literal["low", "medium", "high"]
    next_action: str

    @model_validator(mode="after")
    def _grounded(self):
        if self.category != DiagnosisCategory.INSUFFICIENT_EVIDENCE and not self.cited_observations:
            raise ValueError("a diagnosis other than insufficient_evidence must cite observations")
        if self.category == DiagnosisCategory.INSUFFICIENT_EVIDENCE and self.uncertainty != "high":
            raise ValueError("insufficient_evidence must carry high uncertainty")
        return self


# -------------------------------------------------------------------- patch
ALLOWED_PATCH_PREFIXES = ("src/payment/", "src/checkout/", "src/frontend/")


class Patch(Contract):
    patch_id: Identifier
    base_revision: GitRevision
    diff_sha256: Sha256
    affected_files: Annotated[list[str], Field(min_length=1)]
    rationale: str
    approved_diff_sha256: Optional[Sha256] = None
    tests: list[str] = []
    image_digests: dict[str, str] = {}
    rollback_images: dict[str, str] = {}

    @field_validator("affected_files")
    @classmethod
    def _paths_in_scope(cls, files):
        for name in files:
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"patch path must be relative without '..': {name}")
            if not name.startswith(ALLOWED_PATCH_PREFIXES):
                raise ValueError(f"patch path outside allowed application paths: {name}")
        return files

    @property
    def approved(self) -> bool:
        return self.approved_diff_sha256 == self.diff_sha256


# --------------------------------------------------------------- comparison
class PhaseSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    attempts: Annotated[int, Field(ge=0)]
    successes: Annotated[int, Field(ge=0)]
    missing_samples: Annotated[int, Field(ge=0)]
    p95_latency_ms: Optional[float] = None


class Comparison(Contract):
    comparison_id: Identifier
    original_run_id: Identifier
    replay_run_id: Optional[Identifier] = None
    baseline: PhaseSummary
    fault: PhaseSummary
    recovery: PhaseSummary
    differences: list[str] = []
    limitations: list[str] = []
    conclusive: bool
