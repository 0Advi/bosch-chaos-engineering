"""Operator CLI: python -m controller.cli {capabilities,preflight,plan,run,probe,baseline,
compare-baselines,derive-policy}"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import subprocess

from pydantic import ValidationError

from .agent.tools import ToolRegistry
from .baseline import check as baseline_check, policy as payment_policy
from .baseline.measure import Environment, load_criteria, measure
from .baseline.runtime import DockerRuntime, upstream_revision
from .catalog import Unsupported
from .executor import runner
from .observer import endpoints
from .observer.fixtures import protected_probes, run_attempt
from .observer.http import Shop
from .observer.observer import observe_checkout
from .observer.telemetry import Telemetry, opensearch_log_search
from .safety import sandbox
from .schemas import Plan, SandboxRef


def live_environment(timeout: float, logs: bool = True) -> Environment:
    """Wire the observer to the running loopback sandbox (read-only)."""
    spec = sandbox.load_spec()
    context = sandbox._docker("context", "show").strip()
    daemon = sandbox._docker("info", "--format", "{{.ID}}").strip()
    return Environment(
        shop=Shop(endpoints.app_url(), timeout),
        telemetry=Telemetry(endpoints.jaeger_api(), endpoints.metrics_url(),
                            log_search=opensearch_log_search(spec["compose_project"]) if logs else None),
        sandbox=SandboxRef(docker_context=context, daemon_id=daemon, compose_project=spec["compose_project"]),
        runtime=DockerRuntime(spec["compose_project"]), upstream_revision=upstream_revision(),
        log=lambda line: print(line, file=sys.stderr, flush=True))


def _param(text: str):
    key, _, value = text.partition("=")
    if value.lower() in ("true", "false"):
        return key, value.lower() == "true"
    return key, int(value) if value.lstrip("-").isdigit() else value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("capabilities", help="list the capability catalog")
    sub.add_parser("preflight", help="check sandbox identity and scope")
    plan = sub.add_parser("plan", help="propose a validated plan (no execution)")
    plan.add_argument("capability_id")
    plan.add_argument("-p", "--param", action="append", default=[], type=_param)
    plan.add_argument("--duration", type=int, default=15)
    plan.add_argument("--impact", default="checkout fails while protected paths stay healthy")
    plan.add_argument("--baseline-ref")
    plan.add_argument("-o", "--out", type=Path)
    run = sub.add_parser("run", help="preview a plan; --live is unavailable in P0")
    run.add_argument("plan_file", type=Path)
    run.add_argument("--live", action="store_true")
    probe = sub.add_parser("probe", help="one valid checkout + protected probes, correlated with telemetry")
    probe.add_argument("--fixture", default="valid",
                       choices=["valid", "invalid_number", "expired", "unsupported_type"])
    base = sub.add_parser("baseline", help="measure a healthy baseline (P1-03)")
    base.add_argument("--attempts", type=int)
    base.add_argument("--duration", type=float, help="seconds over which attempts are spread")
    base.add_argument("--warmup", type=int)
    base.add_argument("--no-logs", action="store_true", help="skip OpenSearch log correlation")
    cmp_ = sub.add_parser("compare-baselines", help="check two baselines agree")
    cmp_.add_argument("baselines", nargs=2, type=Path)
    derive = sub.add_parser("derive-policy", help="derive config/payment_policy.yaml (P1-04)")
    derive.add_argument("baselines", nargs="+", type=Path)
    derive.add_argument("--write", action="store_true", help="write config/payment_policy.yaml")
    args = parser.parse_args(argv)

    tools = ToolRegistry()
    try:
        if args.cmd == "capabilities":
            out = tools.dispatch("list_capabilities", {})
        elif args.cmd == "preflight":
            out = sandbox.evaluate(sandbox.collect_snapshot(), sandbox.load_spec()).as_dict()
        elif args.cmd == "plan":
            out = tools.dispatch("propose_plan", {
                "capability_id": args.capability_id, "parameters": dict(args.param),
                "duration_seconds": args.duration, "expected_impact": args.impact})
            if out["ok"]:
                out["result"]["requested_by"] = "user"
                if args.baseline_ref:
                    out["result"]["baseline_ref"] = args.baseline_ref
                if args.out:
                    args.out.write_text(json.dumps(out["result"], indent=2) + "\n")
        elif args.cmd == "run":
            out = runner.execute(Plan.model_validate_json(args.plan_file.read_text()), live=args.live)
        elif args.cmd == "probe":
            criteria = load_criteria()["workload"]
            env = live_environment(criteria["request_timeout_seconds"])
            record = run_attempt(env.shop, args.fixture, "baseline")
            obs, detail = observe_checkout(record, env.telemetry, "probe-manual", criteria["trace_wait_seconds"])
            out = {"ok": bool(record.get("matched")) and obs.complete, "attempt": record,
                   "observation": obs.model_dump(mode="json"), "telemetry": detail,
                   "protected": protected_probes(env.shop)}
        elif args.cmd == "baseline":
            criteria = load_criteria()
            env = live_environment(criteria["workload"]["request_timeout_seconds"], logs=not args.no_logs)
            result, path = measure(env, criteria, args.attempts, args.duration, args.warmup,
                                   check_logs=not args.no_logs)
            out = {"ok": result.healthy, "baseline": str(path), "healthy": result.healthy,
                   "blocking": result.blocking, "probes": [p.model_dump() for p in result.probes],
                   "telemetry": result.telemetry}
        elif args.cmd == "compare-baselines":
            a, b = (baseline_check.load(p) for p in args.baselines)
            problems = baseline_check.reproducibility_problems(a, b, load_criteria()["reproducibility"])
            out = {"ok": not problems, "reproducible": not problems, "problems": problems}
        else:
            loaded = [baseline_check.load(p) for p in args.baselines]
            derived = payment_policy.derive(loaded, load_criteria(), args.baselines)
            out = {"ok": True, "policy": derived.model_dump(mode="json")}
            if args.write:
                out["written"] = str(payment_policy.write(derived))
    except Unsupported as refusal:
        out = {"ok": False, **refusal.as_dict()}
    except payment_policy.PolicyError as error:
        out = {"ok": False, "error": "POLICY_NOT_DERIVABLE", "message": str(error)}
    except ValidationError as error:
        if args.cmd == "run":
            out = {"ok": False, "error": "INVALID_PLAN", "message": "plan does not match the contract"}
        else:
            out = {"ok": False, "error": "INVALID_INPUT", "message": str(error)[:500]}
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        out = {"ok": False, "error": "ENVIRONMENT_UNAVAILABLE",
               "message": "Docker inspection failed or timed out; sandbox approval is unverified"}
    except (OSError, ValueError) as error:
        out = {"ok": False, "error": "INVALID_INPUT", "message": str(error)[:500]}
    print(json.dumps(out, indent=2, default=str))
    failed = out.get("ok") is False or out.get("isolation_approved") is False
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
