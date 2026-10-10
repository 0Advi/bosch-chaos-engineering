"""Bounded tool registry exposed to the model (P0-04).

The model only ever names a capability ID and typed parameters. Tool inputs
are strict pydantic models, so free-form commands, paths, URLs, containers or
flag names are rejected before any controller code runs. Every tool returns
{"ok": true, "result": ...} or {"ok": false, "error": CODE, "message": ...}.
"""
from __future__ import annotations

import json
from typing import Annotated, Callable, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..catalog import Catalog, Unsupported, load_policy
from ..safety import sandbox
from ..schemas import ParamValue, Plan, SandboxRef

MAX_ARGUMENT_BYTES = 4096
MAX_CALLS_PER_SESSION = 40


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoArgs(Args):
    pass


class DescribeArgs(Args):
    capability_id: Annotated[str, Field(max_length=64)]


class ProposePlanArgs(Args):
    capability_id: Annotated[str, Field(max_length=64)]
    parameters: dict[Annotated[str, Field(max_length=32)], ParamValue] = {}
    duration_seconds: Annotated[int, Field(ge=0, le=30, strict=True)] = 15
    expected_impact: Annotated[str, Field(min_length=1, max_length=500)]


class ValidatePlanArgs(Args):
    plan: dict


class RunRefArgs(Args):
    run_id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")]
    after_seq: Annotated[int, Field(ge=0)] = 0


class Tool(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    name: str
    description: str
    args: type[Args]
    mutates: bool
    handler: Callable


def _ok(result):
    return {"ok": True, "result": result}


def _err(code: str, message: str, **details):
    return {"ok": False, "error": code, "message": message, **details}


class ToolRegistry:
    def __init__(self, catalog: Optional[Catalog] = None, policy: Optional[dict] = None,
                 spec: Optional[dict] = None):
        self.catalog = catalog or Catalog()
        self.policy = policy or load_policy()
        self.spec = spec or sandbox.load_spec()
        self.calls = 0
        self.tools = {t.name: t for t in [
            Tool(name="list_capabilities", description="List supported, preview and disabled fault capabilities.",
                 args=NoArgs, mutates=False, handler=self._list),
            Tool(name="describe_capability", description="Full typed contract for one capability.",
                 args=DescribeArgs, mutates=False, handler=self._describe),
            Tool(name="propose_plan", description="Build a validated plan for a capability. Does not execute.",
                 args=ProposePlanArgs, mutates=False, handler=self._propose),
            Tool(name="validate_plan", description="Validate a plan document against contracts and policy.",
                 args=ValidatePlanArgs, mutates=False, handler=self._validate),
            Tool(name="run_plan", description="Execute a validated plan (controller-gated).",
                 args=RunRefArgs, mutates=True, handler=self._not_in_phase),
            Tool(name="get_run_events", description="Read ordered run events after a sequence number.",
                 args=RunRefArgs, mutates=False, handler=self._not_in_phase),
            Tool(name="abort_run", description="Request abort and restoration of an active run.",
                 args=RunRefArgs, mutates=True, handler=self._not_in_phase),
        ]}

    # ---------------------------------------------------------- public API
    def definitions(self) -> list[dict]:
        """Provider-neutral tool definitions (name, description, JSON Schema)."""
        return [{"name": t.name, "description": t.description,
                 "input_schema": t.args.model_json_schema()} for t in self.tools.values()]

    def dispatch(self, name: str, arguments: dict | str) -> dict:
        self.calls += 1
        if self.calls > MAX_CALLS_PER_SESSION:
            return _err("TOOL_BUDGET_EXCEEDED", f"more than {MAX_CALLS_PER_SESSION} tool calls")
        if name not in self.tools:
            return _err("UNKNOWN_TOOL", f"no tool named {name!r}", tools=sorted(self.tools))
        raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
        if len(raw.encode()) > MAX_ARGUMENT_BYTES:
            return _err("ARGUMENTS_TOO_LARGE", f"arguments exceed {MAX_ARGUMENT_BYTES} bytes")
        tool = self.tools[name]
        try:
            args = tool.args.model_validate_json(raw)
        except ValidationError as error:
            return _err("INVALID_ARGUMENTS", "arguments do not match the tool schema",
                        problems=[f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in error.errors()])
        try:
            return _ok(tool.handler(args))
        except Unsupported as refusal:
            return {"ok": False, **refusal.as_dict()}

    # ------------------------------------------------------------ handlers
    def _list(self, _):
        return {"catalog_version": self.catalog.version, "capabilities": self.catalog.summaries()}

    def _describe(self, args: DescribeArgs):
        return self.catalog.get(args.capability_id).model_dump(mode="json", exclude={"adapter_config"})

    def _propose(self, args: ProposePlanArgs):
        cap = self.catalog.plannable(args.capability_id)
        try:
            params = cap.check_parameters(args.parameters)
        except ValueError as error:
            raise Unsupported("PARAMETER_OUT_OF_BOUNDS", str(error),
                              parameters=[p.model_dump() for p in cap.parameters])
        if args.duration_seconds > cap.duration.max_seconds:
            raise Unsupported("DURATION_TOO_LONG",
                              f"{cap.id} allows at most {cap.duration.max_seconds}s")
        plan = Plan(plan_id=f"plan-{uuid4().hex[:12]}", request_id=f"req-{uuid4().hex[:12]}",
                    capability_id=cap.id, parameters=params,
                    sandbox=SandboxRef(docker_context=self.spec["docker_context"],
                                       daemon_id=self.spec["daemon_id"] or "unset",
                                       compose_project=self.spec["compose_project"]),
                    duration_seconds=args.duration_seconds, expected_impact=args.expected_impact,
                    policy_version=self.policy["policy_version"], requested_by="agent")
        return plan.model_dump(mode="json")

    def _validate(self, args: ValidatePlanArgs):
        try:
            plan = Plan.model_validate(args.plan)
        except ValidationError as error:
            raise Unsupported("INVALID_PLAN", "plan does not match the Plan contract",
                              problems=[f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in error.errors()])
        from ..executor.runner import validate
        cap, params = validate(plan, self.catalog, self.policy)
        return {"valid": True, "capability_id": cap.id, "parameters": params}

    def _not_in_phase(self, _):
        raise Unsupported("NOT_AVAILABLE_IN_PHASE",
                          "Execution and run inspection are exposed to the model after P3/P4; "
                          "an operator runs plans through the controller CLI.")
