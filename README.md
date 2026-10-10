# Bosch Chaos Engineering

Repository foundation for a controlled, observable chaos-engineering demonstration.
It combines a pinned OpenTelemetry Astronomy Shop with its existing metrics,
traces, logs, and workload generator. A separate Python controller will add
deterministic safety, bounded fault execution, recovery, and evidence reports.

**Current milestone: local shop, cart/checkout, and telemetry startup verified.
See IMPLEMENTATION_STATUS.md for evidence and remaining work.**
There is no executable fault command yet.

## Selected components

- Shop: Envoy proxy, Next.js frontend/API, ad, recommendation, product catalog,
  cart, checkout, currency, payment, email, shipping, quote, and image provider.
- State: PostgreSQL (`astronomy-db`) and Valkey (`valkey-cart`).
- Workload and supporting services: Locust, flagd, flagd-ui, and telemetry-docs.
- Observation: OpenTelemetry Collector, Prometheus, Jaeger, OpenSearch, Grafana,
  and OpAMP server.

The selected upstream minimal deployment contains **25 services**. Kafka,
accounting, fraud detection, the shopping AI agent/MCP/chatbot, and profiling
services are not enabled. Their code remains available in the pinned upstream
submodule without becoming runtime dependencies of this configuration.

Recommendation is the proposed first bounded outage target. It is a separate,
stateless feature path; upstream classifies it as medium criticality. Confirm its
actual impact before allowing an experiment. The controller's initial contract
is in [config/experiments.yaml](config/experiments.yaml), with execution disabled.

## Layout

```text
vendor/opentelemetry-demo/      Pinned Apache-2.0 upstream Git submodule
deploy/                        Selected Compose overlay and Collector adjustment
scripts/demo.py                Deployment validation/start/stop/smoke helper
scripts/verify_startup.py       Synthetic checkout and telemetry startup checks
config/experiments.yaml         Proposed disabled experiment and safety contract
controller/README.md           Next-phase controller responsibilities
artifacts/dependency_map.static.json  Static, unobserved architecture seed
docs/architecture-review.md     Source-based architecture and selection rationale
IMPLEMENTATION_STATUS.md        Evidence, limitations, and next work
docs/local-preview.md           Local URLs and a walkthrough of the current demo
```

## Prerequisites and isolation

- Python 3.10+, Git, Make, Docker with its daemon running, and Docker Compose
  **2.24.4+** (needed for overlay reset/override tags).
- A dedicated local sandbox Docker environment. The controller will need to
  verify that environment before a future experiment.
- Upstream estimates about 3 GB RAM for minimal mode and 14 GB disk. Building all
  language images may require additional time, memory, disk and network access.
  Actual usage has not been measured for this project.

The overlay removes fixed upstream container names, uses the
`bosch-chaos-sandbox` project/network, removes backend host ports, and exposes
only the shop and Prometheus on `127.0.0.1`. This reduces collisions and exposure;
it is not a substitute for a dedicated sandbox daemon. The upstream Collector
still mounts the Docker socket and host filesystem for observation. A read-only
socket mount does not limit Docker API capabilities.

## Obtain and validate

```sh
git clone --recurse-submodules https://github.com/0Advi/bosch-chaos-engineering.git
cd bosch-chaos-engineering
make validate
```

If cloned without submodules, run `make bootstrap`. The helper refuses a changed
upstream commit and checks selected services, absence of fixed container names,
loopback-only ports, versioned-image policy, project network, and static-map scope.
It does not need a running Docker daemon for configuration validation.

Upstream source is pinned to official release **3.1.0**, commit
`dedc0178918e260823323b8d95005a8cb924b007`. Default startup pulls matching
`ghcr.io/open-telemetry/demo:3.1.0-<service>` images instead of floating `latest`
tags. External infrastructure image versions come from the pinned upstream
`.env`. Record resolved image digests during runtime verification for stronger
deployment reproducibility.

For development source builds, use `make build` and then
`BOSCH_SOURCE_BUILD=true make start`. These use separate
`bosch-local/demo:3.1.0-source-<service>` image names so official images are not
overwritten. Source builds take longer than the default preview startup.

## Start, check, and stop

Use only the dedicated sandbox runtime:

```sh
make start
make status
make smoke
make verify
```

`make start` downloads the selected images and waits up to 300 seconds for
Compose startup/health checks after container creation. Image downloads are not
bounded by that startup wait. Browser-driven Locust traffic is disabled by
default to reduce memory use; plain HTTP traffic still exercises shop APIs.
OpenSearch has a 180-second startup grace period because its JVM initialization
on this machine exceeded the upstream health check's short allowance.
The sandbox workload adapter filters out the upstream shopping-agent task because
that service is excluded. Collector batching uses a one-second timeout, and
host/container resource polling uses a 30-second interval to reduce local overhead.
Short-experiment abort decisions will need faster application probes.

| Interface | Local URL |
|---|---|
| Shop | http://localhost:8080/ |
| Grafana | http://localhost:8080/grafana/ |
| Jaeger | http://localhost:8080/jaeger/ui/ |
| Locust | http://localhost:8080/loadgen/ |
| Feature flags | http://localhost:8080/feature/ |
| Prometheus | http://localhost:9090/ |

Change `BOSCH_APP_PORT` or `BOSCH_METRICS_PORT` in `deploy/demo.local.env` if needed
(this machine-specific file is ignored by Git), or set the corresponding
environment variables for each helper invocation. On the preparation machine,
the local override uses shop port **8081** because 8080 belongs to another app,
and `LOCUST_USERS=1` to reduce resource pressure during the preview.
The official Docker Hub mirror can also be selected with
`IMAGE_NAME=docker.io/otel/demo` in that file; the same release is published by
upstream to both registries. The preparation machine uses this mirror because
its downloads completed faster than GitHub Container Registry.
`make smoke` checks the home page, products, recommendations, and Prometheus
readiness. It is not an end-to-end checkout test or a healthy baseline.

`make verify` adds a synthetic demo cart/checkout, checks Grafana, and waits for
traces, metrics and indexed logs. It saves `artifacts/startup_verification.json`.
It is a startup check, not a baseline, and does not execute a fault. Further
verify telemetry freshness and expected synchronous trace edges before any fault. Reuse
upstream frontend and telemetry tests as appropriate. Its `make` telemetry-test
targets manage their own deployment/teardown and should not be run unchanged
against this customized deployment.

```sh
make stop
```

Stop tears down this Compose project without requesting volume deletion. Keep
the upstream random feature-flag scheduler idle and failure flags at their
resting settings during baseline and Bosch experiments.

## Next milestone

After startup succeeds, implement discovery and a repeatable baseline, followed
by the deterministic controller described in [controller/README.md](controller/README.md).
It must enforce a target allowlist, one active experiment, healthy baseline,
hard deadline, threshold abort, missing-observation handling, manual kill switch,
automatic restoration, and verified application recovery.

Fast bounded API probes are needed for short-experiment decisions; existing
metrics export/view intervals can be too slow. Measure affected recommendation
requests separately from protected shop workflows. Save real baseline, fault,
rollback and recovery observations with timestamps and source identifiers.

The static map is intentionally named `dependency_map.static.json`. It does not
prove runtime dependencies or satisfy the measured Day 1 checkpoint.

## License and references

Apache-2.0; see [LICENSE](LICENSE) and [NOTICE](NOTICE). Upstream attribution and
licenses remain in the submodule.

- [OpenTelemetry Demo](https://github.com/open-telemetry/opentelemetry-demo)
- [Official architecture](https://opentelemetry.io/docs/demo/architecture/)
- [Official Docker deployment](https://opentelemetry.io/docs/demo/docker-deployment/)
- [Implementation status](IMPLEMENTATION_STATUS.md)
