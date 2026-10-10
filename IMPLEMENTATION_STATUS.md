# Implementation status

Updated 10 October 2026. Local startup verification passed at **11:21 IST**.

## Completed

- Public repository and licensed, pinned OpenTelemetry Astronomy Shop source.
- Core shop and existing observability stack: exactly 25 services.
- Official release 3.1.0 source at `dedc0178918e260823323b8d95005a8cb924b007`;
  matching versioned application images, with image IDs/digests captured.
- Separate Compose project/network and loopback-only published shop/metrics ports.
- Existing Collector, Prometheus, Jaeger, OpenSearch, Grafana, and Locust deployed.
- Static architecture seed and proposed experiment/safety contract; faults disabled.
- Repeatable configuration, workflow, and telemetry startup checks.

## Verified runtime evidence

See [artifacts/startup_verification.json](artifacts/startup_verification.json).
This is a single timestamped startup verification, not a measured baseline.

- All 25 containers running; defined health checks passed. No OOM kills or
  automatic restart counts were recorded in that snapshot.
- Browser visibly rendered the catalog; the API returned all 10 products.
- Home, catalog, recommendations, currencies, cart add/read, demo checkout,
  and empty-cart-after-checkout checks passed.
- Synthetic checkout returned an order and items in approximately 2.81 seconds.
- Grafana and Prometheus readiness endpoints passed.
- Jaeger reported 16 trace services; Prometheus reported 18 named metric services.
- OpenSearch contained 2,447 indexed log records at verification time.
- Configuration validation, Python compilation, and whitespace checks passed.
- Upstream working tree remains unchanged. No fault was executed.

## Local preview

Open http://localhost:8081/ for the shop, `/grafana/` for dashboards,
`/jaeger/ui/` for traces, `/loadgen/` for generated traffic, and
http://localhost:9090/ for Prometheus. See [docs/local-preview.md](docs/local-preview.md).

Machine-specific settings use port 8081, the official Docker Hub release mirror,
one simulated HTTP user, and no browser-driven load. A fresh clone defaults to
port 8080, GHCR, and five HTTP users; sustainable load on another machine has
not been established.

Initial startup exposed a short OpenSearch health-check allowance, resource
pressure, intermittent gateway timeouts, and generated requests to the excluded
shopping-agent service. The overlay now provides a longer startup grace period,
a shop-only workload adapter, one-second telemetry batching, and 30-second
host/container polling. The latest full workflow passed after these adjustments;
sustained stability still requires baseline measurement.

The preview runs in Docker Desktop on this machine. Its project/network separation
is verified configuration, but this does not establish the dedicated runtime
isolation required before a future fault. The Collector still mounts the Docker
socket and host filesystem for observation.

## Day 1 checklist

| Requirement | Status |
|---|---|
| Public, licensed application selected | Verified |
| Deployment and selected components | Started locally |
| App works end to end in sandbox | Startup workflow passed |
| Metrics, traces, logs | Presence verified; complete freshness checks pending |
| Dependency map | Static seed; runtime edges pending |
| Measured healthy baseline | Pending |
| Controlled fault and visible impact | Pending |
| Threshold abort, kill switch, invalid-target rejection | Pending controller |
| Automatic restoration and recovery verification | Pending |
| One active experiment | Proposed config; enforcement pending |
| Evidence report and reproducible chaos demo | Startup evidence saved; experiment report pending |

## Next action

Measure a repeatable healthy baseline and confirm trace dependencies. Then
implement deterministic safety before enabling one bounded recommendation outage
in a dedicated sandbox. Evaluate the proposed thresholds against actual
measurements. Recommendation/solution generation and before/after comparison
are still future work; the running upstream dashboards do not implement those.
