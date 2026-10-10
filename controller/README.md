# Bosch controller boundary

Controller implementation begins after the sample application runs end to end.
The deployment helper in `scripts/demo.py` exposes no fault execution command.
`config/experiments.yaml` is a proposed contract with execution disabled.

Required modules for the next phase:

| Module | Responsibility | Required evidence |
|---|---|---|
| discovery | Resolve Compose, distinguish startup/call/telemetry edges, merge observed traces and manual corrections | Labelled dependency map |
| baseline | Fixed workload, timestamped latency/error/health measurements and missing-data indicators | Actual baseline JSON |
| observer | Bounded API probes, Prometheus queries, trace/log correlation, observation freshness | Fault and protected-path measurements |
| safety | Allowlist and sandbox identity, healthy baseline, lock, hard deadline, thresholds, manual abort, fail closed | Rejection and threshold-triggered rollback tests |
| executor | Stop/start only one validated existing sandbox container | Container state and verified fault effect |
| report | Compare baseline/fault/recovery, record cleanup and limitations | Reproducible JSON/Markdown report |

Recover through normal completion, cancellation, observation loss, and process
failure; verify successful application requests after restoration. A watchdog
must cover controller termination. An LLM may eventually propose structured
plans but must never obtain unrestricted Docker/shell access.

These modules are documented work to do, not placeholder implementations claimed
as functional. The existing upstream agent, MCP and random flag scheduler do not
replace this controller.
