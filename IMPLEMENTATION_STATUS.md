# Implementation status

Updated 10 October 2026.

## Completed repository foundation

- Inspected upstream code at `e5552ee25bcede2900d89188f076c464cd73c074`.
- Selected the core shop and existing observability stack: 25 services.
- Preserved upstream as a pinned Git submodule with Apache-2.0 attribution.
- Added a deployment overlay removing fixed container names, using a dedicated
  project/network, and restricting published ports to loopback.
- Configured local builds of application images from pinned source.
- Excluded Kafka/full, shopping AI, and profiling layers.
- Removed unused Kafka configuration and Firepit profile export from the selected
  Collector command/configuration layers.
- Added a static dependency-map seed, architecture review, reproducible setup
  commands, disabled experiment contract, and controller implementation boundary.
- Added configuration validation and CI for those setup checks.

## Verification evidence

- GitHub CLI authenticated as `0Advi`.
- Docker Compose version: `v2.31.0-desktop.2` on the preparation machine.
- Runtime preflight: Docker reported that its daemon was not running.
- `make validate` passed: pinned revision, exact 25-service selection, no fixed
  container names, source-build image policy, project network, and loopback-only
  host port publication. The check resolved real Docker Compose configuration
  without starting containers.
- `scripts/demo.py --help` passed; static-map services match resolved Compose.
- `git diff --check` passed; upstream working tree remained unchanged.
- No application images built; no containers started; no fault executed.

## Day 1 checklist

| Requirement | Status |
|---|---|
| Public, licensed application selected | Verified from upstream source |
| Deployment instructions and selected components | Prepared; runtime not verified |
| App works end to end in sandbox | Pending startup |
| Dependency map | Static seed only; runtime confirmation pending |
| Measured healthy baseline | Pending |
| Controlled fault and visible impact | Pending |
| Threshold-triggered abort and invalid-target rejection | Pending controller |
| Cleanup and recovery verification | Pending |
| No concurrent experiments | Proposed config; enforcement pending |
| Evidence report and reproducible full demo | Pending |

## Next action

Start Docker in a dedicated sandbox, run `make start`, and verify application
workflows and telemetry. Only then implement baseline and deterministic safety.
The supplied threshold values are proposals and must be evaluated against actual
measurements. Repository preparation is not a claim that the Bosch milestone is
already achieved.
