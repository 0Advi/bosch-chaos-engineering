#!/usr/bin/env python3
"""Offline, read-only release inventory. Runtime acceptance is a separate gate."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from controller.catalog import Catalog, load_policy
from controller.safety.sandbox import load_spec
from controller.executor.flag_store import baseline_problems, FLAG_FILE


def main():
    """Check release readiness: pinned upstream revision, clean flags, derived policy and an
    approved sandbox spec.
    """
    expected = 'dedc0178918e260823323b8d95005a8cb924b007'
    actual = subprocess.check_output(['git', '-C', str(ROOT/'vendor/opentelemetry-demo'), 'rev-parse', 'HEAD'], text=True).strip()
    problems = baseline_problems(FLAG_FILE)
    if actual != expected: problems.append('Pinned upstream revision differs.')
    policy, spec = load_policy(), load_spec()
    runtime = []
    if not spec.get('approved'): runtime.append('Dedicated Docker sandbox is not approved.')
    if not spec.get('daemon_id'): runtime.append('Approved Docker daemon identity is missing.')
    if not policy.get('execution_enabled'): runtime.append('Live execution is disabled.')
    if not (ROOT/'config/payment_policy.yaml').is_file(): runtime.append('Two healthy measured baselines and derived policy are required.')
    runtime += ['Real abort/watchdog recovery demonstration requires dedicated-sandbox acceptance.',
                'Application error fix, immutable-image deployment operator and measured replay require acceptance.']
    out = {'kind':'offline_release_inventory','upstream_revision':actual,
           'catalog_capabilities':len(Catalog().by_id),
           'source_components': ['controller','workbench','improvements','evals','schemas','deploy'],
           'flag_sha256':hashlib.sha256(FLAG_FILE.read_bytes()).hexdigest(),
           'packaging_ok':not problems,'packaging_problems':problems,
           'live_acceptance_complete':False,'runtime_remaining':runtime,
           'limitations':['This inventory does not execute a fault, prove a baseline, or certify deployment.']}
    print(json.dumps(out, indent=2))
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
