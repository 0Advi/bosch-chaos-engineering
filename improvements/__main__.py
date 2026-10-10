"""Operator-only isolated patch preparation; no test command or deployment endpoint."""
import argparse
import json
from pathlib import Path

from .patches import prepare, structural_checks


def main():
    parser = argparse.ArgumentParser(description='Prepare a reviewable patch without editing pinned upstream')
    parser.add_argument('--diff', type=Path, required=True)
    parser.add_argument('--patch-id', required=True)
    parser.add_argument('--base', default='dedc0178918e260823323b8d95005a8cb924b007')
    parser.add_argument('--rationale', required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    # Validate identity before deriving a workspace path.
    from controller.schemas.contracts import Patch
    from .patches import inspect_diff, sha256
    data = args.diff.read_bytes()
    Patch(patch_id=args.patch_id, base_revision=args.base, diff_sha256=sha256(data),
          affected_files=inspect_diff(data), rationale=args.rationale)
    workspace = root / 'artifacts' / 'patch-workspaces' / args.patch_id
    patch = prepare(root / 'vendor/opentelemetry-demo', workspace, data,
                    args.patch_id, args.base, args.rationale)
    record = {'patch': patch.model_dump(mode='json'), 'checkout': str(workspace),
              'checks': structural_checks(workspace, patch),
              'application_tests': 'not_run', 'runtime_evidence': 'not_observed',
              'deployment': 'blocked', 'approval': None}
    report = root / 'artifacts' / 'patch-workspaces' / (args.patch_id + '.json')
    report.write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps(record, indent=2))


if __name__ == '__main__':
    main()
