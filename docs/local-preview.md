# Local preview and next work

This machine uses port 8081 because port 8080 is already occupied. A fresh clone
defaults to 8080; override it in `deploy/demo.local.env` when needed.

| Open | What to inspect |
|---|---|
| http://localhost:8081/ | Astronomy Shop, the application we will test |
| http://localhost:8081/grafana/ | Existing dashboards for service metrics and logs |
| http://localhost:8081/jaeger/ui/ | Traces showing which services handled requests |
| http://localhost:8081/loadgen/ | Locust's generated requests, failures, and response times |
| http://localhost:9090/ | Prometheus metrics and queries |

## Walk through the application

1. Open the shop and scroll to **Hot Products**.
2. Pick a product, add it to the cart, and open the cart. This is a synthetic demo;
   use its supplied test data for checkout.
3. Open Jaeger, select `frontend`, and find recent traces. Expand a trace to see
   calls to the catalog, cart, recommendation, and other services.
4. Open Grafana's dashboards and Locust. This machine uses one simulated HTTP
   user to fit its resource capacity; a fresh clone defaults to five. Browser-driven
   load is disabled. Measure sustainable throughput before choosing experiment load.
5. In Prometheus, query `count by (service_name) (target_info)` to see services
   exporting metrics. Startup evidence is saved in
   `artifacts/startup_verification.json`; it records the latest verification run.

## What this preview proves

Use `IMPLEMENTATION_STATUS.md` and the timestamped evidence files for actual
verification results. A working shop and telemetry stack provide the foundation
for the chaos experiment. The static dependency map has not yet been confirmed
edge by edge from runtime traces.

## Move forward

1. Measure a repeatable healthy baseline: request success rate and latency,
   recommendation response quality, and protected cart/checkout workflows.
2. Confirm service dependencies from traces and determine which observations
   arrive fast enough for a short experiment.
3. Implement the controller's target allowlist, one-experiment lock, deadline,
   threshold abort, manual stop, automatic restoration, and recovery checks.
4. Run one bounded recommendation outage in a dedicated sandbox runtime and save
   before/during/after evidence.
5. Generate evidence-based suggestions, apply a chosen fix, and repeat the same
   experiment to compare outcomes.

Fault execution is disabled. The current checkout check and telemetry counts
do not replace the baseline or demonstrate abort/recovery behavior.

## Manage this preview

From the repository directory, use `make status`, `make smoke`, and `make verify`
to inspect it. `make stop` stops this Compose project and retains its volumes.
`make start` starts it again; downloaded images are reused.
