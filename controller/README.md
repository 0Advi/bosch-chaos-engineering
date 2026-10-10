# Bosch controller boundary

Phase 0 implements versioned shared records and JSON Schema exports, legal lifecycle
transitions and resumable event sequences, a typed payment capability catalog,
bounded proposal tools, sandbox scope checks, and validated CLI previews.
`executor/flag_store.py` snapshots and restores exact flag variant/state through
atomic file replacement. The viewer mounts the same flag directory read-only.

Live execution is unconditionally blocked. The runner/adapters are deferred
scaffolding, not accepted P2 execution. Changing an approval or execution boolean
cannot enable faults. The flag verifier also refuses its mutation roundtrip.
No external model or API key is needed for these deterministic tools.

## Remaining responsibilities

| Phase | Responsibility | Required evidence |
|---|---|---|
| P1 | Repeatable synthetic checkout, observations, fresh telemetry and measured baseline | Healthy workload and observed dependencies |
| P2 | Approved runtime, single-run lock, thresholds, deadline, manual abort and independent watchdog | Invalid-target refusal and recovery through process termination |
| P3 | Run-linked records and reports | Correlated baseline/fault/recovery evidence |
| P4 | Evidence-grounded diagnosis and suggestion tools | Blind evaluations and missing-data handling |
| P5–P7 | Workbench, reviewed application fixes and matched replay | Demonstrated recovery and measured improvement |

The current preview is not approved isolation. See [sandbox setup](../docs/sandbox.md)
and [Phase 0 review](../docs/phase0-review.md). Run tests with the pinned dependencies
in `requirements-dev.txt`; no running Docker daemon is required for unit tests.
