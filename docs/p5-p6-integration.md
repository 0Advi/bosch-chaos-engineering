# P5/P6 parallel preparation and handoff

Branch: `codex/p5-p6-preparation`, based on reviewed Phase 0 main `4cd84b5`.
P1–P4 remain owned by the teammate. This branch adds separate `workbench/` and
`improvements/` modules and does not modify controller contracts or the upstream
submodule. The previously published Sites visualization remains a separate concept.

## Start and verify

```sh
. .venv/bin/activate
make workbench
# Open http://127.0.0.1:8095
```

The Python fixture server binds only to loopback. It validates plans through the
Phase 0 tool registry; every run, abort, approval and deployment mutation is refused.
All saved run observations, diagnoses and comparisons shown here are illustrative.
No model key, Docker daemon or package installation is required for the workbench.

```sh
make test
# Node 20+ (CI uses Node 22)
make test-workbench
python -m controller.schemas.export --check
```

For failure-state UI testing, restart the server with one of:
`python -m workbench.server --case cleanup-failed`, `--case stale`,
`--case provider-error`, or `--case api-error`. The normal case is the default.

## Preparation status versus acceptance

| Task | Ready in this branch | Still required after P1–P4 |
|---|---|---|
| P5-01 | Capability inputs, validated plans, sandbox/readiness inspector and disabled-action explanations | Authoritative real readiness and live plan creation |
| P5-02 | Ordered timeline, cursor resume/deduplication, observations, diagnosis, recovery, history, restore control | Real event API, stream/poll consistency and sandbox abort verification |
| P5-03 | Exact diff/hash/base, reviewer, tests, approval/deployment status and comparison screens | Authenticated revision approval, actual build/deployment and measured replay |
| P5-04 | Deterministic state/transport tests, failure fixtures and manual browser checks | Recorded real-sandbox end-to-end flow |
| P6-01 | Source-based RPC-code preservation candidate, ready for isolated inspection | Reproduce misclassification, implement typed payment/frontend categories and validate cart handling |
| P6-02 | Scope-limited text patch preparation in a separate checkout; base/diff/source hashes and structural checks | Application tests, approved test receipt, explicitly built source tree and bounded agent-tool registration |
| P6-03 | Exact revision/test/image/target approval gates and fake-tested image transaction/rollback | Persistent P2 lock/watchdog, operator adapter, real immutable image records and crash-recovery evidence |
| P6-04 | Matched workload/policy/target checks; improvement/regression/unverified outcomes | Original and replay observations from the same capability and verified deployment |

P5/P6 acceptance checkboxes remain open. Fixtures and fake operators establish
integration behavior, not evidence of live execution or a verified payment fix.

## Proposed API seam

Serve the static workbench from the P3 controller's origin. `workbench/api.mjs` uses
only same-origin `/api/workbench`. Add a thin adapter to existing P3 routes rather
than implementing a second runner. Responses use Phase 0's envelope:
`{"ok":true,"result":...}` or `{"ok":false,"error":"CODE","message":"..."}`.
All input validation, idempotency, readiness and execution policy remain server-owned.

| Method/path after `/api/workbench` | Input / result |
|---|---|
| GET empty suffix | `{mode:"controller", readiness, capabilities:Capability[], history:Run[]}` |
| POST `/plans/validate` | Typed capability ID, parameters, duration, expected impact → `{plan:Plan}` |
| POST `/runs` | `{plan:Plan}` → persisted Run; enforce Idempotency-Key |
| GET `/runs/{run_id}` | `{run:Run, observations:Observation[], diagnosis:Diagnosis|null, patch_review, comparison}` |
| GET `/runs/{run_id}/events?after_seq=N` | `{events:Event[]}`; immutable sequences, gaps cause full reload |
| POST `/runs/{run_id}/abort` | `{reason:"manual_abort"}`; idempotent restore request, independent supervision |
| POST `/patches/{patch_id}/approve` | Exact diff/base/test digest, target and reviewer; authenticate reviewer server-side |
| POST `/patches/{patch_id}/deploy` | Exact diff hash and server-issued approval; revalidate under experiment/deployment lock |

`readiness` contains `sandbox:SandboxRef`, booleans `execution_enabled`,
`isolation_approved`, `baseline_healthy`, `watchdog_ready`, `live_capabilities:string[]`,
`blockers:string[]`, and `protected_path_status:string`. Require literal true values.
Return only authoritative fresh state. UI enablement is convenience, not authorization.
A disconnected UI must never stop backend supervision. A restore request remains
available on controller mode for active/pending-cleanup states, including stale state.

`patch_review` contains `patch:Patch`, `diff:string`, `tests:{passed,checks,digest,...}`,
`approval:null|{diff_sha256,...}`, `deployment_ready:boolean`, `deployment:null|{outcome,...}`.
Comparison presentation expects `{original_run_id,replay_run_id,conclusive,outcome,
differences:{measure:{before,after}},limitations:[]}`. This is a view projection of
P3/P6 reports, not a replacement for the Phase 0 Comparison schema.

The browser preserves selected run ID and mutation request keys in session storage;
saved records and cursor data come from the backend after refresh. It polls operational
views every five seconds. A push/event-stream adapter can replace polling later.

## Isolated patch workflow

```sh
python -m improvements \
  --diff patches/checkout-preserve-rpc-code.candidate.patch \
  --patch-id patch-checkout-review-01 \
  --rationale 'Source-based candidate pending runtime reproduction'
```

Output is an ignored `artifacts/patch-workspaces/<patch-id>` checkout and review JSON.
Existing workspaces are refused. The pinned upstream checkout is unchanged. Only
existing regular text files under `src/payment/`, `src/checkout/`, `src/frontend/`
are accepted. Renames, symlinks, binary changes, file creation/deletion, mode changes,
path escapes and excessive patches are refused in this initial version.
`structural_checks` refuses edited source/base/diff and untracked build inputs.

The candidate changes two checkout lines: wrap payment errors with `%w` and retain
RPC status at the checkout boundary. It is deliberately provisional: generic payment
exceptions can still be UNKNOWN, and frontend mapping remains incomplete. Do not
present it as a complete typed payment fix or deploy it before runtime findings.
Preparation/whitespace checks do not constitute application tests.

`improvements.lifecycle.approve` binds review to base, diff, application test receipt,
new/rollback images and sandbox identity. Any changed revision/test result/target
invalidates approval. The caller must issue receipts from real bounded application
checks and authenticate reviewers. No approval API is implemented by the fixture server.
The default deployment backend refuses all work. `TransactionalBackend` provides
normal-failure rollback logic through a trusted operator protocol. Its operator must
supply persistent exclusive locks, fresh runtime/idle/recovery checks, health deadlines,
durable journaling and independent crash recovery from P2/P3. In-memory receipts or
fake operators do not meet those requirements.

Replay comparison requires matched capability parameters, workload digest/version,
policy and target; separate runs and changed application revision/images; measured,
fresh, complete observations and verified cleanup/recovery. Better error categories
cannot override regressions or turn a persistent outage into a successful payment.
The caller must derive these inputs from saved observations, not model assertions.

## Merge sequence

1. Review and merge teammate P1–P4 with its acceptance evidence.
2. Merge this preparation branch, resolving only shared documentation/CI/Makefile edits.
3. Connect P3 routes through the seam above and run the full contract/UI tests.
4. Reproduce the payment category issue; approve the supported patch and run application tests/builds.
5. Connect P2's locked/watchdog-supervised operator to P6, then record deployment,
   rollback and matched replay checks in the dedicated sandbox.
6. Mark P5/P6 tasks complete only when their runtime acceptance evidence exists.

## Preparation verification, 10 October 2026

- 137 Python tests passed, including all 93 Phase 0 tests.
- Six browser-independent JavaScript state/transport tests passed on bundled Node.
- Eight shared schema exports remain unchanged and current.
- Browser checks covered all five views, plan validation, disabled run/approve/deploy
  actions, saved-run selection after refresh and keyboard submission with a 12-second plan.
- A narrow mobile viewport had no page-level horizontal overflow; responsive navigation
  and form controls remained usable. The temporary viewport override was reset.
- The candidate applied in a separate pinned checkout and passed source/hash,
  whitespace and Go formatting checks. Application tests/build and runtime evaluation
  were not run. No upstream change, image replacement or fault occurred.
