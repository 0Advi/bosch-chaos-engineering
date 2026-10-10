"""Operator CLI: python -m controller.cli {capabilities,preflight,plan,run}"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .agent.tools import ToolRegistry
from .catalog import Unsupported
from .executor import runner
from .safety import sandbox
from .schemas import Plan


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
    run = sub.add_parser("run", help="dry-run a plan; --live executes if every gate passes")
    run.add_argument("plan_file", type=Path)
    run.add_argument("--live", action="store_true")
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
        else:
            out = runner.execute(Plan.model_validate_json(args.plan_file.read_text()), live=args.live)
    except Unsupported as refusal:
        out = {"ok": False, **refusal.as_dict()}
    print(json.dumps(out, indent=2, default=str))
    failed = out.get("ok") is False or out.get("isolation_approved") is False
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
