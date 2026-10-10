"""P4-02: agent tools share the controller's enforcement; sessions are bounded.
Also P4-01: a failing model never affects runs (API diagnosis endpoint)."""
import json
import shutil
import threading
import time

import pytest

from controller.agent.providers import FakeProvider, ProviderError
from controller.agent.session import Budget, run_session
from controller.agent.tools import ToolRegistry
from controller.api import ControllerAPI, serve
from controller.executor.flag_store import FLAG_FILE
from tests.fake_env import FakeEnvironment, fast_limits
from tests.test_api import Client, wait_terminal
from tests.test_agent_diagnosis import proposal


@pytest.fixture
def api(tmp_path):
    flag = tmp_path / "flagd" / "demo.flagd.json"
    flag.parent.mkdir()
    shutil.copy(FLAG_FILE, flag)
    gates = {"blockers": []}
    controller = ControllerAPI(db_path=tmp_path / "results" / "runs.sqlite3", results_dir=tmp_path / "results",
                               env_factory=lambda: FakeEnvironment(flag), limits=fast_limits(),
                               gate_check=lambda p, c: list(gates["blockers"]), root=tmp_path,
                               baselines_dir=tmp_path / "artifacts" / "baselines", stream_poll=0.05)
    return controller, flag, gates


def agent_plan(registry, **params):
    out = registry.dispatch("propose_plan", {"capability_id": "payment-unreachable", "parameters": params,
                                             "duration_seconds": 1, "expected_impact": "checkout fails"})
    assert out["ok"], out
    return out["result"]


def wait_done(controller, run_id):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        view = controller.run_summary(run_id)
        if view["terminal"] or view["state"] == "NEEDS_ATTENTION":
            return view
        time.sleep(0.05)
    raise AssertionError("run did not finish")


# --------------------------------------------------------------- registry
def test_read_only_registry_allows_dry_runs_only():
    registry = ToolRegistry()
    plan = agent_plan(registry)
    dry = registry.dispatch("run_plan", {"plan": plan})
    assert dry["ok"] and dry["result"]["mode"] == "dry_run"
    assert registry.dispatch("run_plan", {"plan": plan, "live": True})["error"] == "MUTATION_NOT_PERMITTED"
    assert registry.dispatch("capture_baseline", {})["error"] == "MUTATION_NOT_PERMITTED"
    assert registry.dispatch("get_run", {"run_id": "run-1"})["error"] == "NOT_CONFIGURED"


def test_inspect_path_exposes_edges_not_source_text():
    out = ToolRegistry().dispatch("inspect_path", {})
    assert out["ok"] and any(e.startswith("checkout->payment") for e in out["result"]["edges"])
    assert "connectivity" in out["result"]["categories"] and "func (cs" not in json.dumps(out)


def test_agent_runs_use_the_same_gates_store_and_request_ids(api):
    controller, flag, gates = api
    registry = ToolRegistry(controller=controller, allow_mutation=True)
    plan = agent_plan(registry)
    started = registry.dispatch("run_plan", {"plan": plan, "live": True})
    assert started["ok"], started
    run_id = started["result"]["run_id"]
    dup = registry.dispatch("run_plan", {"plan": plan, "live": True})
    assert dup["error"] == "DUPLICATE_REQUEST" and dup["run_id"] == run_id
    assert wait_done(controller, run_id)["state"] == "RECOVERED"
    assert flag.read_bytes() == FLAG_FILE.read_bytes()
    assert registry.dispatch("get_run", {"run_id": run_id})["result"]["cleanup_status"] == "verified"
    events = registry.dispatch("get_run_events", {"run_id": run_id, "after_seq": 3})["result"]["events"]
    assert events[0]["seq"] == 4
    report = registry.dispatch("get_report", {"run_id": run_id})["result"]
    assert report["run_id"] == run_id and report["conclusive"]
    assert registry.dispatch("abort_run", {"run_id": run_id})["error"] == "NOT_ACTIVE"

    gates["blockers"] = ["sandbox: not approved"]
    blocked = registry.dispatch("run_plan", {"plan": agent_plan(registry), "live": True})
    assert blocked["error"] == "LIVE_EXECUTION_BLOCKED" and blocked["blockers"]


def test_agent_cannot_submit_user_attributed_plans(api):
    controller, *_ = api
    registry = ToolRegistry(controller=controller, allow_mutation=True)
    plan = agent_plan(registry) | {"requested_by": "user"}
    out = registry.dispatch("run_plan", {"plan": plan, "live": True})
    assert out["error"] == "INVALID_PLAN"


# ---------------------------------------------------------------- session
def test_session_relays_refusals_and_executes_nothing():
    plan = agent_plan(ToolRegistry())
    fake = FakeProvider([
        {"tool_calls": [{"id": "t1", "name": "run_plan", "input": {"plan": plan, "live": True}},
                        {"id": "t2", "name": "shell", "input": {"cmd": "docker rm -f payment"}}]},
        "That is not supported from this read-only session."])
    result = run_session("break payment", fake, ToolRegistry())
    assert result.ok and "not supported" in result.text
    assert [(c["name"], c["error"]) for c in result.tool_calls] == [
        ("run_plan", "MUTATION_NOT_PERMITTED"), ("shell", "UNKNOWN_TOOL")]
    tool_results = fake.requests[1].messages[-1]["content"]
    assert all(r["is_error"] for r in tool_results) and {r["tool_use_id"] for r in tool_results} == {"t1", "t2"}


def test_session_budgets_and_provider_errors_end_cleanly():
    looping = FakeProvider(responder=lambda req: {"tool_calls": [
        {"id": f"t{len(req.messages)}", "name": "list_capabilities", "input": {}}]})
    result = run_session("loop", looping, ToolRegistry(), Budget(max_model_calls=3))
    assert not result.ok and result.error["kind"] == "budget" and len(result.tool_calls) == 3
    failing = FakeProvider([ProviderError("overloaded", "busy", True)])
    result = run_session("hi", failing, ToolRegistry())
    assert not result.ok and result.error["kind"] == "overloaded"


# ------------------------------------------------------- API + diagnosis
def test_diagnosis_endpoint_and_model_failure_isolation(api):
    controller, *_ = api
    controller.provider_factory = lambda: FakeProvider([{"json": proposal(cited_evidence=["ev-001"])}])
    server = serve(controller, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = Client(server.server_address[1])
    try:
        plan = agent_plan(ToolRegistry())
        status, run = client.call("POST", "/api/v1/runs", {"plan": plan, "live": True})
        assert client.call("POST", f"/api/v1/runs/{run['run_id']}/diagnosis", {})[0] in (200, 409)
        final = wait_terminal(client, run["run_id"])
        status, out = client.call("POST", f"/api/v1/runs/{run['run_id']}/diagnosis", {"diagnoser": "model"})
        assert status == 200 and out["diagnosis"]["diagnoser"] == "model"
        assert out["diagnosis"]["status"] in ("grounded", "unverified")
        status, saved = client.call("GET", f"/api/v1/runs/{run['run_id']}/diagnosis")
        assert saved["diagnosis"]["result_id"] == out["diagnosis"]["result_id"]
        files = client.call("GET", f"/api/v1/runs/{run['run_id']}/evidence")[1]["files"]
        assert "diagnosis.json" in files

        controller.provider_factory = lambda: FakeProvider([ProviderError("auth", "bad key")])
        status, failed = client.call("POST", f"/api/v1/runs/{run['run_id']}/diagnosis", {})
        assert status == 502 and failed["diagnosis"]["status"] == "failed"
        after = client.call("GET", f"/api/v1/runs/{run['run_id']}")[1]
        assert (after["state"], after["last_seq"]) == (final["state"], final["last_seq"])   # run untouched

        status, base = client.call("POST", f"/api/v1/runs/{run['run_id']}/diagnosis", {"diagnoser": "baseline"})
        assert status == 200 and base["diagnosis"]["diagnoser"] == "baseline"
    finally:
        server.shutdown()
        server.server_close()
