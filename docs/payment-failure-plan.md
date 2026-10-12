# Payment failure experiments and agent evaluation

Source review: 10 October 2026, pinned OpenTelemetry Demo 3.1.0,
`dedc0178918e260823323b8d95005a8cb924b007`.
This is a proposed experiment design. No fault was executed in this review.
The executable controller, healthy baseline, and recovery safeguards remain work
in progress. The early experiment contract permitted only a proposed
recommendation stop and has execution disabled; this document does not enable it.

## Target application and payment flow

Use the already deployed local Astronomy Shop. Its payment service validates
synthetic card data and returns a random transaction ID. The inspected code does
not call a bank or payment processor or maintain a durable transaction ledger.
The `demo.payment.charged` attribute is a demo convention, not evidence of a real
bank settlement. Send `baggage: synthetic_request=true` with experiment requests
and use only the upstream fake fixtures; keep `emitRawPii` off.

The important synchronous sequence is:

`POST /api/checkout -> checkout PlaceOrder -> cart/catalog/currency + shipping quote
-> payment Charge -> ship order -> empty cart -> return order`

Email is attempted after the order is constructed, with a bounded timeout; an
email failure is logged rather than making the order fail. A shipping failure
AFTER Charge can fail the checkout after the simulated payment succeeded. A
frontend response/proxy timeout also does not establish that payment failed.

## Scenario selection

| Scenario | Where and how | Current support | Evidence to look for | Suggested improvement |
|---|---|---|---|---|
| Payment handler rejects requests | `paymentFailure`, variant `100%` (numeric value 1), in payment `charge.js` | Built in; best first deterministic fault | Payment `charge` and RPC error, exception text `Invalid token`, checkout failure; payment process still healthy | Restore flag, then typed service-error classification and an accurate checkout message |
| Checkout cannot reach payment | `paymentUnreachable=on`, in checkout `chargeCard`; calls `badAddress:50051` | Built in; best second fault | Checkout's outgoing Charge span with connection/DNS error; payment can remain healthy | Restore routing; validate endpoint/DNS, bound RPC timeouts, preserve cart |
| Invalid or expired card | Change one field in an upstream fake checkout fixture | Built in; negative business test, not a service outage | Validation exception in payment, declined checkout, cart retained | Specific validation message; do not retry a permanent decline |
| Actual payment process outage | Stop only the validated sandbox payment container for a bounded interval | No guarded executor yet | Container/health down plus failed checkout-to-payment RPC | Restore same container; graceful service-unavailable handling and bounded recovery |
| Payment latency or lost response | A scoped test hook or proxy on checkout-to-payment; a post-success response-loss hook for ambiguous outcome | New code/tooling needed; no built-in payment delay flag found | Long Charge span or timeout; possibly successful payment-side transaction despite checkout failure | Deadline hierarchy; reconcile outcome before retrying |
| Duplicate simulated transaction | Repeat the same logical order after a lost response | Requires a durable fake ledger and idempotency key before it can be evaluated meaningfully | More than one transaction for the same logical order/key | Persistent idempotency, transaction lookup, reconciliation |

Partial `paymentFailure` variants use randomness, so observed rates will vary.
Start with the deterministic `100%` variant, then evaluate partial failures with
larger samples. The pinned `90%` variant actually contains `0.95`: use numeric
values and record them; do not assume the label is exact.

`paymentUnreachable` changes the caller's address. It does not stop payment or
simulate packet loss. The observer must distinguish those mechanisms.

## Useful findings in the inspected implementation

1. Payment throws ordinary JavaScript errors and passes them to the gRPC callback.
   Checkout wraps charge failures into an Internal error, while the frontend
   classifies them using substrings in the error details. Source inspection
   predicts an injected generic payment error may become HTTP 422/PAYMENT_FAILED
   and tell the user to check card details. Confirm this at runtime: the desired
   fix is a structured distinction between card decline, provider/service failure,
   and unknown outcome, preserved across payment, checkout, and frontend.
2. `demo.payment.transactions` increments only after a successful simulated
   payment. It is not an attempts counter, so it cannot alone provide payment
   failure rate. Count all eligible attempts with probes/RPC observations and
   supplement them with this success counter.
3. Payment's health service stays SERVING during application-level charge errors.
   Container health passing does not imply payment requests succeed.
4. Each unreachable-flag call creates a new gRPC client connection in checkout
   without an explicit close in that path. Keep the first experiment small;
   connection reuse/cleanup is an additional hardening candidate.
5. The request/response protocol and payment implementation have no explicit
   idempotency key or persistent deduplication ledger. Blind payment retries
   cannot be recommended as a demonstrated safe fix.
6. Synthetic success records can have `demo.payment.charged=false`. Do not label
   those as failures; use the RPC/checkout outcome and transaction evidence.

## Agent tools and experiment contract

The LLM proposes a catalog scenario ID and explains observations. A deterministic
runner validates the proposal and owns mutations; it does not give the LLM
arbitrary shell/Docker commands. The proposed catalog is
the early proposed scenario list (now `config/capabilities.yaml`), with execution disabled.

Recommended bounded tools:

- `inspect_payment_path`: read the pinned dependency/error-mapping information.
- `capture_payment_baseline`: fixed valid fixtures, one controlled workload,
  latency/success/cart/telemetry measurements, timestamps and freshness.
- `run_payment_scenario(scenario_id)`: accept allowlisted IDs only; manage flag
  snapshot, scope checks, deadline, lock, watchdog, and restoration.
- `observe_payment_run(run_id)`: return probe records, trace/span IDs, sanitized
  log references, applicable metrics, health, and missing-data indicators.
- `abort_payment_run(run_id)`: restore the prior state and verify recovery.
- `report_payment_run(run_id)`: compare baseline/fault/recovery and link evidence.

For flag scenarios, make a dedicated copy of upstream flag configuration under
our deployment directory and mount the same copy into flagd and flagd-ui. The
current deployment mounts files from the upstream submodule; do not automate
edits there. Update only the chosen default variant, preserve the prior exact
value/hash, verify that the new value was evaluated, and verify the actual fault
with requests. Restore that prior value, not an assumed `off` value. Block manual
UI/scheduler writes during a run or detect conflicting changes and stop safely.
The rollback/watchdog must cover cancellation and runner termination, including
persisted restoration information for recovery after restart.

## Evaluation procedure

1. Stabilize the current preview. Earlier startup recorded gateway timeouts;
   first collect a healthy payment baseline with both flags off. Suggested
   initial baseline: at least 30 valid attempts over about three minutes, with
   a fixed workload. These are proposals, not measured pass criteria.
2. Derive request timeout and abort thresholds from that baseline. The earlier
   successful checkout took 2.81 seconds, so the existing proposed two-second
   timeout is unsuitable for payment without further measurement/tuning. Keep
   the application deadline below the proxy deadline and the experiment deadline.
3. Create a fresh cart/session for each attempt. Probe checkout with valid fake
   data and synthetic baggage, and collect correlated trace IDs. Use fixed
   products/currency/address so validation or shipping differences are controlled.
4. Apply one fault with a proposed 15-second target duration and a hard cap no
   greater than 30 seconds. If too few requests finish, report insufficient
   evidence rather than relaxing safety or inventing an error rate. Three
   deterministic failing requests can demonstrate the mechanism, not estimate
   real-world reliability. Restore on completion, manual abort, observation loss,
   protected-path degradation, or deadline expiry.
5. During payment faults, checkout is the AFFECTED path. Protect home/catalog,
   cart reads/adds, and observability. Do not copy recommendation's abort policy:
   intended checkout errors are not themselves evidence that protected paths
   degraded. Record downstream shipping/order effects and cart retention.
6. Restore the exact prior state and verify successful checkout, empty cart,
   telemetry freshness, and protected paths. Flag restoration alone is not proof
   of application recovery.
7. Implement one chosen application fix in a controlled source build and repeat
   the same scenario/workload. Distinguish restored infrastructure from improved
   failure handling. A persistent simulated outage should still prevent checkout;
   success can mean correct messaging, preserved cart, bounded latency, and no
   duplicate transaction, rather than a fabricated successful payment.

## Diagnose from correlated evidence

A 504 alone is ambiguous: locate the failing/slow span and check whether payment
already succeeded. Payment-side errors while payment is healthy suggest a handler
failure/decline; an outgoing Charge connection error with no corresponding server
span suggests routing/connectivity only if telemetry freshness/completeness are
established. A missing span alone is not proof of an outage. A transaction success
followed by shipping or response failure means an ambiguous/partial order outcome,
not necessarily a failed payment.

Record `run_id`, scenario, prior/applied/restored configuration, actual times,
request/trace/span IDs, HTTP and RPC status, error category, latency, cart state,
protected-path results, rollback reason, recovery, and uncertainty. Preserve
sanitized evidence references, not raw card/CVV/email payloads.

For evaluation, keep the injector's manifest as ground truth. Ask the diagnostic
agent to classify from observations before revealing the selected injection, then
compare its answer with that manifest. Evaluate detection delay, correct failing
service/edge, error category, impact scope, evidence quality, recovery, and the
relevance of the proposed fix. For partial failures report attempts and confidence
limits; for missing telemetry report uncertainty. Evaluate suggestions by the
same repeated experiment, not how convincing their wording sounds.

## Current readiness and next implementation

Available: deployed payment/checkout, two fault switches, fake fixtures,
cart/checkout checks, traces/logs/metrics, and a health endpoint.
Pending: stable baseline, isolated flag storage, guarded flag executor, fresh
correlated observers, payment-aware thresholds, watchdog/verified restoration,
structured error taxonomy, and evidence-based report rules. Payment delay,
lost-response, and duplicate-payment evaluation need additional test hooks or a
fake ledger.

Recommended order: deterministic payment handler failure, caller-side unreachable
payment, validation control case; then latency/ambiguous outcome/idempotency.
Do not broaden the active experiment allowlist until the runner and safety are
implemented and validated. This is an evaluation design, not a completed fault run.

## Primary source references

- Pinned source: `src/payment/charge.js`, `src/payment/index.js`,
  `src/checkout/main.go`, `src/frontend/pages/api/checkout.ts`,
  `src/flagd/demo.flagd.json`, and `pb/demo.proto` in the upstream submodule.
- https://opentelemetry.io/docs/demo/feature-flags/
- https://opentelemetry.io/docs/demo/services/payment/

Official online docs describe the general mechanisms; concrete behavior in this
plan comes from the pinned source. Runtime outcomes described as predictions
must be confirmed by the future guarded experiment.
