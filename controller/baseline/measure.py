"""Measure a healthy payment baseline (P1-03).

warmup -> evenly spaced valid checkouts with protected probes -> invalid-card
controls -> trace/log correlation -> metric freshness -> verdict. All attempts
count. The result is saved even when it is unhealthy, with blocking findings.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from typing import Callable, Optional

import yaml

from ..executor.flag_store import FLAG_FILE, baseline_problems
from ..observer.fixtures import CONTROLS, PROTECTED_PROBES, SECRETS, WORKLOAD_VERSION, PRODUCT_ID, \
    CURRENCY, protected_probes, run_attempt
from ..observer.http import Shop
from ..observer.observer import observe_checkout, observe_probe
from ..observer.sanitize import assert_clean
from ..observer.telemetry import Telemetry
from ..schemas import Baseline, SandboxRef
from .runtime import health_findings
from .stats import percentile, probe_stats

ROOT = Path(__file__).resolve().parents[2]
CRITERIA_PATH = ROOT / "config" / "baseline_criteria.yaml"
OUT_DIR = ROOT / "artifacts" / "baselines"
CHECKOUT = "POST /api/checkout"


def load_criteria(path: Path = CRITERIA_PATH) -> dict:
    return yaml.safe_load(path.read_text())


@dataclass
class Environment:
    shop: Shop
    telemetry: Optional[Telemetry]
    sandbox: SandboxRef
    runtime: Optional[object] = None          # DockerRuntime-like: inventory(), stats()
    upstream_revision: str = "unknown"
    flag_file: Path = FLAG_FILE
    out_dir: Path = OUT_DIR
    log: Callable[[str], None] = print


def _sleep_until(deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)


def measure(env: Environment, criteria: dict, attempts: Optional[int] = None,
            duration_seconds: Optional[float] = None, warmup: Optional[int] = None,
            check_logs: bool = True) -> tuple[Baseline, Path]:
    w = criteria["workload"]
    attempts = attempts if attempts is not None else w["attempts"]
    duration = duration_seconds if duration_seconds is not None else w["duration_seconds"]
    warmup = warmup if warmup is not None else w["warmup_attempts"]
    trace_wait = w["trace_wait_seconds"]
    started = datetime.now(timezone.utc)
    baseline_id = f"baseline-{started:%Y%m%d-%H%M%S}"
    limitations, blocking = [], []

    flag_issues = baseline_problems(env.flag_file)
    blocking += [f"flags not at baseline: {p}" for p in flag_issues]
    health_before = env.runtime.inventory() if env.runtime else None
    resources_before = env.runtime.stats() if env.runtime else None

    # Warmup: recorded, excluded from statistics; also measures trace ingestion delay.
    warm_records, ingest_delays = [], []
    for i in range(warmup):
        record = run_attempt(env.shop, "valid", "warmup")
        obs, detail = observe_checkout(record, env.telemetry, baseline_id, trace_wait, check_logs=False)
        warm_records.append((record, obs))
        if detail.get("trace_wait_seconds") is not None:
            ingest_delays.append(detail["trace_wait_seconds"])
        env.log(f"warmup {i + 1}/{warmup}: HTTP {record.get('http_status')} {record.get('latency_ms')} ms")

    # Measurement: evenly spaced attempts, protected probes after each.
    measured, probes = [], []
    interval = duration / attempts if attempts else 0
    t0 = time.monotonic()
    for i in range(attempts):
        _sleep_until(t0 + i * interval)
        record = run_attempt(env.shop, "valid", "baseline")
        measured.append(record)
        probes += protected_probes(env.shop)
        env.log(f"attempt {i + 1}/{attempts}: HTTP {record.get('http_status')} "
                f"{record.get('latency_ms')} ms matched={record.get('matched')}")
    controls = [run_attempt(env.shop, name, "control") for name in CONTROLS]

    health_after = env.runtime.inventory() if env.runtime else None
    resources_after = env.runtime.stats() if env.runtime else None

    # Correlate after the workload so trace ingestion runs in parallel with it.
    deadline = time.monotonic() + trace_wait
    observations, details = [], {}
    for record in measured + controls:
        wait = max(1.0, deadline - time.monotonic())
        obs, detail = observe_checkout(record, env.telemetry, baseline_id, wait, check_logs)
        observations.append(obs)
        details[record["attempt_id"]] = detail
    probe_obs = [observe_probe(p, baseline_id, "baseline", i) for i, p in enumerate(probes)]
    metric_ages = env.telemetry.metric_freshness() if env.telemetry else {}

    # ---------------------------------------------------------- statistics
    checkout = probe_stats(CHECKOUT, [(r.get("matched", False), r.get("latency_ms")) for r in measured])
    protected = [probe_stats(name, [(p["ok"], p["latency_ms"]) for p in probes if p["probe"] == name])
                 for name in PROTECTED_PROBES]
    measured_obs = observations[:len(measured)]
    traced = sum(1 for o in measured_obs if "trace" not in o.missing
                 and not any(m.startswith("span:") for m in o.missing))
    logged = sum(1 for o in measured_obs if o.log_refs and not any(m.startswith("logs") for m in o.missing))
    telemetry = {
        "checked": env.telemetry is not None,
        "trace_coverage": round(traced / len(measured), 3) if measured else 0.0,
        "trace_ingest_delay_s": {"samples": len(ingest_delays), "p95": percentile(ingest_delays, 95),
                                 "max": max(ingest_delays) if ingest_delays else None},
        "log_coverage": round(logged / len(measured), 3) if measured and check_logs else None,
        "metric_freshness": metric_ages,
        "missing_counts": _missing_counts(observations),
    }
    control_rows = [{"fixture": r["fixture"], "http_status": r.get("http_status"),
                     "error_code": r.get("error_code"), "cart_items_after": r.get("cart_items_after"),
                     "matched": r.get("matched", False),
                     "observation_complete": o.complete, "missing": o.missing}
                    for r, o in zip(controls, observations[len(measured):])]

    # ------------------------------------------------------------- verdict
    c = criteria
    if checkout.failure_rate > c["valid_checkout_max_failure_rate"]:
        blocking.append(f"valid checkout failure rate {checkout.failure_rate} > "
                        f"{c['valid_checkout_max_failure_rate']} ({checkout.failures}/{checkout.attempts})")
    for p in protected:
        if p.failure_rate > c["protected_max_failure_rate"]:
            blocking.append(f"protected {p.probe} failure rate {p.failure_rate} ({p.failures}/{p.attempts})")
    if c["controls_must_match"]:
        blocking += [f"control {r['fixture']} did not match expectations: HTTP {r['http_status']} "
                     f"{r['error_code']} cart={r['cart_items_after']}" for r in control_rows if not r["matched"]]
    if env.telemetry is None:
        blocking.append("telemetry was not checked")
    else:
        if telemetry["trace_coverage"] < c["min_trace_coverage"]:
            blocking.append(f"trace coverage {telemetry['trace_coverage']} < {c['min_trace_coverage']}")
        for name, m in metric_ages.items():
            if m["age_seconds"] is None:
                blocking.append(f"metric {name} not found: {m['query']}")
            elif m["age_seconds"] > c["max_metric_age_seconds"]:
                blocking.append(f"metric {name} is {m['age_seconds']} s old")
        if check_logs and (telemetry["log_coverage"] or 0) < c["min_log_coverage"]:
            blocking.append(f"log coverage {telemetry['log_coverage']} < {c['min_log_coverage']}")
        if not check_logs:
            limitations.append("log correlation was skipped (--no-logs)")
    if env.runtime is None:
        if c["require_all_services_healthy"]:
            blocking.append("service health was not collected")
    else:
        blocking += health_findings(health_before, health_after, c["max_restarts_during_measurement"])

    limitations += [
        "Single-machine Docker measurement; values are valid only for this host and workload.",
        "Checkout attempts run sequentially; concurrency behavior is not measured here.",
        "Synthetic requests carry synthetic_request=true baggage; payment marks them uncharged.",
    ]
    finished = datetime.now(timezone.utc)
    obs_file = env.out_dir / f"{baseline_id}.observations.jsonl"
    baseline = Baseline(
        baseline_id=baseline_id, started_at=started, finished_at=finished, sandbox=env.sandbox,
        upstream_revision=env.upstream_revision,
        flag_sha256=hashlib.sha256(env.flag_file.read_bytes()).hexdigest(),
        workload={"version": WORKLOAD_VERSION, "product_id": PRODUCT_ID, "currency": CURRENCY,
                  "attempts": attempts, "duration_seconds": duration, "interval_seconds": round(interval, 2),
                  "warmup_attempts": warmup, "controls": list(CONTROLS),
                  "request_timeout_seconds": env.shop.timeout,
                  "warmup_results": [{"http_status": r.get("http_status"), "latency_ms": r.get("latency_ms"),
                                      "matched": r.get("matched", False)} for r, _ in warm_records]},
        probes=[checkout, *protected], controls=control_rows, telemetry=telemetry,
        resources={"before": resources_before, "after": resources_after} if env.runtime else {"collected": False},
        health={"before": health_before, "after": health_after} if env.runtime else {"collected": False},
        criteria={"version": c["criteria_version"], **{k: v for k, v in c.items() if k not in ("schema_version",)}},
        healthy=not blocking, blocking=blocking,
        observations_file=str(obs_file.relative_to(ROOT)) if obs_file.is_relative_to(ROOT) else str(obs_file),
        limitations=limitations)
    return baseline, _write(env.out_dir, baseline, obs_file, measured + controls, observations, probe_obs, details)


def _missing_counts(observations) -> dict:
    counts: dict[str, int] = {}
    for o in observations:
        for m in o.missing:
            key = "checkout_not_sent" if m.startswith("checkout_not_sent") else m
            counts[key] = counts.get(key, 0) + 1
    return counts


def _write(out_dir: Path, baseline: Baseline, obs_file: Path, records, observations, probe_obs, details) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"kind": "attempt", "record": r, "observation": o.model_dump(mode="json"),
                         "telemetry": details.get(r["attempt_id"], {})}, default=str)
             for r, o in zip(records, observations)]
    lines += [json.dumps({"kind": "protected_probe", "observation": o.model_dump(mode="json")})
              for o in probe_obs]
    summary = baseline.model_dump_json(indent=2)
    # Refuse to write evidence that contains fixture card/email/address values.
    assert_clean(lines, SECRETS)
    assert_clean(summary, SECRETS)
    obs_file.write_text("\n".join(lines) + "\n")
    path = out_dir / f"{baseline.baseline_id}.json"
    path.write_text(summary + "\n")
    return path
