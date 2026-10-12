# Workload, observations and healthy baseline (P1)

Everything in P1 is read-only toward the application: synthetic checkouts,
probes and telemetry queries. No flag or container is changed.

## Workflow

```bash
make start verify                 # running sandbox and telemetry present
make probe                        # one correlated checkout; fix any 'missing' signal first
make baseline                     # ~4 min: 5 warmup + 30 checkouts over 180 s + 3 controls
make baseline                     # repeat for reproducibility
make derive-policy BASELINES="artifacts/baselines/baseline-A.json artifacts/baselines/baseline-B.json"
```

Results go to `artifacts/baselines/<id>.json` (summary) and
`<id>.observations.jsonl` (every attempt and probe). Both are sanitized; the
writer refuses to save a file containing fixture card numbers, email or address.
Commit the baselines that the policy is derived from.

## Fixtures (P1-01)

| Fixture | Card | Expected |
|---|---|---|
| `valid` | upstream fake VISA | 200, order ID, cart cleared |
| `invalid_number` | VISA with a broken check digit | 422 `PAYMENT_FAILED`, cart retained |
| `expired` | valid VISA, 1/2020 | 422 `PAYMENT_FAILED`, cart retained |
| `unsupported_type` | AMEX test number | 422 `PAYMENT_FAILED`, cart retained |

Each attempt uses a fresh synthetic user and cart, product `66VCHSJNUP` x1, USD,
`synthetic_request=true` baggage and its own `traceparent`.

## Observations (P1-02)

For each checkout the observer looks up its trace in Jaeger
(`/jaeger/ui/api/traces/<id>` through frontend-proxy) and requires frontend,
checkout and payment spans plus the payment `charge` span. It searches indexed
logs in OpenSearch by trace ID and checks Prometheus freshness for span metrics
and `target_info`. Anything not found is listed in `Observation.missing`, and
the observation is marked incomplete. Missing data is never filled in.

Every attempt counts, including timeouts and refused connections. The payment
success counter is not used as a denominator.

## Healthy envelope (P1-03)

Criteria are in `config/baseline_criteria.yaml`: no failed valid checkouts or
protected probes, all controls as expected, trace coverage ≥ 0.9, log coverage
≥ 0.5, metrics ≤ 180 s old, all services healthy, no restarts. An unhealthy
baseline is still saved with its `blocking` list, and execution stays disabled.
A baseline older than 24 h, from another daemon, or taken with a different flag
file cannot gate a live run.

## Payment policy (P1-04)

`derive-policy` needs at least two healthy baselines that agree (same workload,
daemon and flags; checkout p95 within 1.5x; equal failure rates). It writes
`config/payment_policy.yaml`; every gate records its value, rule and inputs.
The early recommendation-experiment thresholds are not used.

| Gate | Rule |
|---|---|
| checkout probe timeout | max(2 x p99, 3 x p95), rounded up to 0.5 s |
| protected probe timeout | max(1 s, 3 x worst protected p99) |
| probe interval | checkout p95, rounded up to 0.5 s |
| min affected samples | 5 (fewer = inconclusive) |
| fault duration | clamp(5 x probe interval, 15, 30) s |
| protected error rate | max(2 x baseline rate, 1 / protected min samples) |
| protected p95 latency | max(2 x worst p95, 1.5 x worst p99) |
| observation max age | max(5 s, 2 x worst trace ingestion p95) |
| metric max age | 1.5 x oldest metric age in healthy baselines |
| restoration deadline | 3 recovery checks x checkout timeout + observation max age |
