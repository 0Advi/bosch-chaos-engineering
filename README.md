# Bosch Chaos Engineering

Repository foundation for a controlled, observable chaos-engineering demonstration.
It combines a pinned OpenTelemetry Astronomy Shop with its existing metrics,
traces, logs, and workload generator. A separate Python controller will add
deterministic safety, bounded fault execution, recovery, and evidence reports.

**Current milestone: application/observability components assembled; deployment
configuration validated. Application startup and chaos behavior are unverified.**
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
config/experiments.yaml         Proposed disabled experiment and safety contract
controller/README.md           Next-phase controller responsibilities
artifacts/dependency_map.static.json  Static, unobserved architecture seed
docs/architecture-review.md     Source-based architecture and selection rationale
IMPLEMENTATION_STATUS.md        Evidence, limitations, and next work
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
loopback-only ports, source-build policy, project network, and static-map scope.
It does not need a running Docker daemon for configuration validation.

Upstream source is pinned to
`e5552ee25bcede2900d89188f076c464cd73c074`. Application images are built locally
from that source as `bosch-local/demo:e5552ee-<service>` instead of pulled from
floating `latest` tags. External infrastructure image versions come from the
pinned upstream `.env`; record resolved image digests during runtime verification
for stronger deployment reproducibility.

## Start, check, and stop

Use only the dedicated sandbox runtime:

```sh
make start
make status
make smoke
```

`make start` builds the selected application images and waits up to 300 seconds
for Compose startup/health checks after container creation. Image builds are not
bounded by that startup wait. This step has not been executed here.

| Interface | Local URL |
|---|---|
| Shop | http://localhost:8080/ |
| Grafana | http://localhost:8080/grafana/ |
| Jaeger | http://localhost:8080/jaeger/ui/ |
| Locust | http://localhost:8080/loadgen/ |
| Feature flags | http://localhost:8080/feature/ |
| Prometheus | http://localhost:9090/ |

Change `BOSCH_APP_PORT` or `BOSCH_METRICS_PORT` in `deploy/demo.env` if needed, or
set the corresponding environment variables for each helper invocation.
`make smoke` checks the home page, products, recommendations, and Prometheus
readiness. It is not an end-to-end checkout test or a healthy baseline.

Then verify cart/checkout, telemetry arrival and freshness, and expected
synchronous trace edges before any controller implementation or fault. Reuse
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
