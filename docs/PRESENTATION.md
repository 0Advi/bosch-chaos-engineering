# Presenting the Payment Chaos Lab

One live payment fault on the real 25-service Astronomy Shop, its measured
evidence, and an AI SRE agent (LangGraph + a free Groq/Gemini model) that
diagnoses it and recommends fixes. Dashboard: http://127.0.0.1:8099

## One-time setup

```sh
python3 -m venv .venv && . .venv/bin/activate      # or reuse the existing .venv
python -m pip install -r requirements-dev.txt -r demo/requirements.txt
cp .env.example .env                                # then paste a free GROQ_API_KEY
```

Free Groq key: https://console.groq.com/keys (no card). Gemini alternative:
set `GEMINI_API_KEY` from https://aistudio.google.com/apikey instead.

## Before you go on stage (about 5 minutes)

1. Start Docker Desktop. Then `make start` (wait until it returns) and `make smoke`.
2. `.venv/bin/python -m demo.server` (or `make demo PYTHON=.venv/bin/python`).
3. Open http://127.0.0.1:8099. All five header chips must be green,
   including `LLM: groq · …`.
4. Do one rehearsal run. It also warms the shop's caches, so the first
   baseline on stage has no cold-start outlier. Keep this run: it is your
   fallback (footer → *Recorded runs* → Load).

## Talk track (about 3 minutes live)

| Step | Show | Say |
|---|---|---|
| 1 Design | Scenario *Payment service rejects charges*, 50% | "We flip one real feature flag in flagd: the payment service will fail half of its charge calls. Guardrails: 60-second cap, auto-restore, and auto-abort if anything beyond checkout breaks." |
| 2 Hypothesis | Hypothesis panel | "Our expectation: only checkout is affected, customers get a clear retryable error, and we recover within seconds." |
| 3 Run | Click **Run experiment**; watch the dots | "Green dots are real orders from synthetic customers. When the red band starts, the fault is live, and roughly half the orders fail. Home, catalog and cart stay green, so the blast radius is contained." |
| 4 Restore | KPI tiles | "The flag is restored automatically. Time to impact is about 1 second; recovery is under 1 second." |
| 5 Evidence | Trace tab, then Metrics and Logs | "This is the distributed trace of one failed order: payment throws, checkout wraps it, and the frontend returns HTTP 422 PAYMENT_FAILED. Prometheus and the logs show the same count independently." |
| 6 Agent | Agent feed | "Now a LangGraph agent on a free Llama 3.3 70B plans an investigation and calls read-only tools: summary, trace, metrics, logs and source code." |
| 7 Recommendations | Report | "It finds the real problem: a payment-service fault is shown to customers as *check your card details*. That makes a backend outage look like a card decline, so it never pages anyone. Each fix says how re-running this experiment verifies it. The reviewer checks that every claim cites evidence the agent actually retrieved." |

## If something goes wrong

- **Fault still active** (header chip red): click **Abort & restore**. The fault
  is also restored when the server exits or restarts.
- **LLM rate-limited or offline**: load the rehearsal run from the footer. It
  includes the saved analysis and is clearly labelled *Recorded run*.
  *Re-run agent* re-analyses any saved run without touching the shop.
- **Shop unhealthy**: `make status`, then `make start` again.

## What is real and what is not

Real: the flag change, the synthetic orders and their HTTP results, the Jaeger
traces, the Prometheus span metrics, the container logs, and the restoration.
The agent's text is model-generated. Its evidence items are checked against the
tools it actually called, and its quoted percentages are checked against the
measured rates. The recommended code changes have not been applied or replayed
in this demo. `patches/checkout-preserve-rpc-code.candidate.patch` is the
starting point for that.
