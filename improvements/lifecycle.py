"""Pure approval/deployment/replay boundaries for integration with P2/P3.

No Docker implementation is provided. A caller must supply an approved backend;
this module cannot establish isolation, watchdog health or evidence authenticity.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from controller.schemas.contracts import Patch, SandboxRef
from .patches import sha256


def fingerprint(value: dict) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode())


@dataclass(frozen=True)
class TestReceipt:
    base_revision: str
    diff_sha256: str
    checks: tuple[str, ...]
    passed: bool
    log_sha256: str

    def digest(self) -> str:
        return fingerprint(vars(self))


@dataclass(frozen=True)
class Approval:
    patch_id: str
    diff_sha256: str
    base_revision: str
    tests_digest: str
    sandbox_digest: str
    approved_by: str
    images_digest: str


def approve(patch: Patch, tests: TestReceipt, sandbox: SandboxRef, reviewer: str) -> Approval:
    if (not reviewer.strip() or tests.passed is not True or not tests.checks or "application-tests" not in tests.checks
            or tests.base_revision != patch.base_revision or tests.diff_sha256 != patch.diff_sha256):
        raise ValueError('approval requires passing tests for this exact patch and a reviewer')
    return Approval(patch.patch_id, patch.diff_sha256, patch.base_revision,
                    tests.digest(), fingerprint(sandbox.model_dump()), reviewer,
                    fingerprint({"new": patch.image_digests, "rollback": patch.rollback_images}))


@dataclass(frozen=True)
class DeploymentContext:
    sandbox: SandboxRef
    isolation_approved: bool = False
    backend_gates_passed: bool = False
    active_run: bool = True
    cleanup_verified: bool = False
    recovery_verified: bool = False


class DeploymentBackend(Protocol):
    # Implementations must own a persistent exclusive lock shared with experiment
    # creation, identity revalidation, health deadlines and crash supervision.
    def deploy_transaction(self, images: dict[str, str], rollback_images: dict[str, str],
                           sandbox: SandboxRef) -> dict: ...


class DisabledBackend:
    def deploy_transaction(self, *args, **kwargs):
        raise ValueError('live patch deployment unavailable until P2/P3 integration is verified')


def deploy(patch: Patch, tests: TestReceipt, approval: Approval,
           context: DeploymentContext, backend: DeploymentBackend | None = None) -> dict:
    expected = approve(patch, tests, context.sandbox, approval.approved_by)
    if expected != approval:
        raise ValueError('approval is stale: patch, tests, base or target changed')
    if (context.isolation_approved is not True or context.backend_gates_passed is not True
            or context.active_run is not False or context.cleanup_verified is not True or context.recovery_verified is not True):
        raise ValueError('deployment requires approved isolation, backend gates and verified recovery between runs')
    services = {f.split('/')[1] for f in patch.affected_files}
    if (set(patch.image_digests) != services or set(patch.rollback_images) != services
            or not all(re_digest(x) for x in [*patch.image_digests.values(), *patch.rollback_images.values()])):
        raise ValueError('new and rollback image digests must cover exactly affected services')
    result = (backend or DisabledBackend()).deploy_transaction(
        patch.image_digests, patch.rollback_images, context.sandbox)
    if result.get('outcome') not in {'verified', 'rolled_back', 'needs_attention'}:
        raise ValueError('backend returned an invalid deployment outcome')
    return result


def re_digest(image: str) -> bool:
    import re
    return bool(re.fullmatch(r'[^\s@]+@sha256:[0-9a-f]{64}', image))


@dataclass(frozen=True)
class ReplayEvidence:
    run_id: str
    capability_id: str
    parameters_digest: str
    workload_version: str
    workload_digest: str
    policy_version: str
    sandbox_digest: str
    source_revision: str
    image_digests: dict[str, str]
    measured: bool
    fresh: bool
    complete: bool
    cleanup_verified: bool
    recovery_verified: bool
    payment_succeeded_during_outage: bool
    expected_category_observed: bool
    protected_paths_healthy: bool
    cart_retained: bool
    response_within_bound: bool


def compare(original: ReplayEvidence, replay: ReplayEvidence) -> dict:
    matched = ('capability_id', 'parameters_digest', 'workload_version', 'workload_digest',
               'policy_version', 'sandbox_digest')
    limits = [f'unmatched {key}' for key in matched if getattr(original, key) != getattr(replay, key)]
    if original.run_id == replay.run_id:
        limits.append('replay must be a separate recorded run')
    if original.source_revision == replay.source_revision:
        limits.append('no changed application revision')
    if not original.image_digests or not replay.image_digests or original.image_digests == replay.image_digests:
        limits.append('no changed application images')
    for label, evidence in [('original', original), ('replay', replay)]:
        if not all(value is True for value in (evidence.measured, evidence.fresh, evidence.complete,
                    evidence.cleanup_verified, evidence.recovery_verified)):
            limits.append(f'{label} measurements or recovery unverified')
    if replay.payment_succeeded_during_outage is not False:
        limits.append('persistent payment outage unexpectedly reports successful payment')
    quality = ('expected_category_observed', 'protected_paths_healthy', 'cart_retained', 'response_within_bound')
    changes = {key: {'before': getattr(original, key), 'after': getattr(replay, key)} for key in quality}
    improved = any(getattr(original, key) is False and getattr(replay, key) is True for key in quality)
    regressed = any(getattr(original, key) is True and getattr(replay, key) is False for key in quality)
    return {'original_run_id': original.run_id, 'replay_run_id': replay.run_id,
            'conclusive': not limits, 'outcome': 'unverified' if limits else
            ('improved' if improved and not regressed else 'ineffective'),
            'differences': changes, 'limitations': limits}
