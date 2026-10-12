"""P3-02 controller API and P3-03 streamed progress, over real HTTP (fake sandbox)."""
from datetime import datetime, timezone
import http.client
import json
import shutil
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from controller.api import ControllerAPI, serve
from controller.executor.flag_store import FLAG_FILE
from controller.schemas import Baseline, ProbeStats, SandboxRef
from controller.store import RunStore
from tests.fake_env import FakeEnvironment, fast_limits, make_plan


class Client:
    """Small HTTP client for the controller API under test."""
    def __init__(self, port):
        """Target the API on a loopback port."""
        self.port, self.base = port, f"http://127.0.0.1:{port}"

    def call(self, method, path, body=None, headers=None):
        """Make a JSON request and return (status, parsed body)."""
        data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
        hdrs = {"Content-Type": "application/json"} if method == "POST" else {}
        hdrs.update(headers or {})
        try:
            with urlopen(Request(self.base + path, data=data, method=method, headers=hdrs), timeout=20) as r:
                raw, status, ctype = r.read(), r.status, r.headers.get("Content-Type", "")
        except HTTPError as error:
            raw, status, ctype = error.read(), error.code, error.headers.get("Content-Type", "")
        return status, (json.loads(raw) if "json" in ctype else raw.decode())

    def raw(self, method, path, body=b"", headers=None):
        """Make a raw request with full control over headers (for Host/Origin checks)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        conn.putrequest(method, path, skip_host=True)
        for k, v in {"Host": f"127.0.0.1:{self.port}", "Content-Length": str(len(body)), **(headers or {})}.items():
            conn.putheader(k, v)
        conn.endheaders(body)
        resp = conn.getresponse()
        return resp.status, resp.read(), dict(resp.getheaders())

    def stream(self, run_id, last_event_id=None, stop_after=None):
        """Read SSE until 'end' (or stop_after events); returns (events, end_payload)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        headers = {"Last-Event-ID": str(last_event_id)} if last_event_id is not None else {}
        conn.request("GET", f"/api/v1/runs/{run_id}/stream", headers=headers)
        resp = conn.getresponse()
        assert resp.status == 200 and resp.getheader("Content-Type") == "text/event-stream"
        events, block = [], {}
        while True:
            line = resp.fp.readline().decode()
            if not line:
                return events, None
            line = line.rstrip("\n")
            if line == "":
                if block.get("event") == "end":
                    return events, json.loads(block["data"])
                if "id" in block:
                    events.append((int(block["id"]), block["event"], json.loads(block["data"])))
                    if stop_after and len(events) >= stop_after:
                        conn.close()                       # browser tab closed mid-run
                        return events, None
                block = {}
            elif not line.startswith(":"):
                key, _, value = line.partition(": ")
                block[key] = value


@pytest.fixture
def setup(tmp_path):
    """Fixture: start the controller API on a free port with a fake environment and temp files."""
    flag = tmp_path / "flagd" / "demo.flagd.json"
    flag.parent.mkdir()
    shutil.copy(FLAG_FILE, flag)
    options = {"gate_blockers": []}
    api = ControllerAPI(db_path=tmp_path / "results" / "runs.sqlite3", results_dir=tmp_path / "results",
                        env_factory=lambda: FakeEnvironment(flag), limits=fast_limits(),
                        gate_check=lambda plan, cap: list(options["gate_blockers"]),
                        allowed_origins=("http://localhost:5173",), baselines_dir=tmp_path / "artifacts" / "baselines",
                        root=tmp_path, stream_poll=0.05)
    server = serve(api, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield Client(server.server_address[1]), api, flag, options
    server.shutdown()
    server.server_close()


def plan_body(duration=1, **kw):
    """A valid plan as JSON."""
    return make_plan(duration=duration, **kw).model_dump(mode="json")


def wait_terminal(client, run_id, timeout=20):
    """Poll a run until it reaches a terminal state."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _, run = client.call("GET", f"/api/v1/runs/{run_id}")
        if run["terminal"] or run["state"] == "NEEDS_ATTENTION":
            return run
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def wait_running(client, run_id):
    """Poll a run until it is RUNNING."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        _, events = client.call("GET", f"/api/v1/runs/{run_id}/events")
        if any(e["type"] == "action_applied" for e in events["events"]):
            return
        time.sleep(0.05)
    raise AssertionError("run never started its fault")


# ------------------------------------------------------------------ P3-02
def test_read_endpoints(setup):
    """Health, catalog, policy and run-list endpoints answer."""
    client, *_ = setup
    status, health = client.call("GET", "/api/v1/health")
    assert status == 200 and health["active_runs"] == [] and health["policy_version"]
    status, caps = client.call("GET", "/api/v1/capabilities")
    assert status == 200 and len(caps["result"]["capabilities"]) >= 6
    status, cap = client.call("GET", "/api/v1/capabilities/payment-unreachable")
    assert status == 200 and "adapter_config" not in cap["result"]
    assert client.call("GET", "/api/v1/capabilities/kill-database")[0] == 404
    status, pre = client.call("GET", "/api/v1/preflight")
    assert status == 200 and pre["isolation_approved"] is True               # fake sandbox


def test_plan_proposal_validation_and_dry_run(setup):
    """Plans can be proposed, validated and dry-run through the API."""
    client, *_ = setup
    status, out = client.call("POST", "/api/v1/plans", {
        "capability_id": "payment-handler-failure", "parameters": {"failure_rate": "100%"},
        "duration_seconds": 15, "expected_impact": "checkout fails"})
    assert status == 201 and out["plan"]["requested_by"] == "user"
    status, dry = client.call("POST", "/api/v1/plans/validate", {"plan": out["plan"]})
    assert status == 200 and dry["mode"] == "dry_run" and dry["actions"]
    status, dry = client.call("POST", "/api/v1/runs", {"plan": out["plan"], "live": False})
    assert status == 200 and dry["mode"] == "dry_run"
    bad = client.call("POST", "/api/v1/plans", {"capability_id": "payment-unreachable",
                                                "expected_impact": "x", "command": "rm -rf /"})
    assert bad[0] == 400 and bad[1]["error"] == "INVALID_ARGUMENTS"
    out_of_bounds = client.call("POST", "/api/v1/plans", {"capability_id": "payment-handler-failure",
                                                          "parameters": {"failure_rate": "250%"},
                                                          "expected_impact": "x"})
    assert out_of_bounds[0] == 422 and out_of_bounds[1]["error"] == "PARAMETER_OUT_OF_BOUNDS"


def test_live_run_lifecycle_report_and_evidence(setup):
    """A live run goes through its lifecycle, restores the flag and exposes its report and evidence."""
    client, _, flag, _ = setup
    status, run = client.call("POST", "/api/v1/runs", {"plan": plan_body(), "live": True})
    assert status == 202 and run["state"] in ("DRAFT", "BASELINING", "READY", "ARMED", "RUNNING")
    final = wait_terminal(client, run["run_id"])
    assert (final["state"], final["cleanup_status"]) == ("RECOVERED", "verified")
    assert flag.read_bytes() == FLAG_FILE.read_bytes()
    status, report = client.call("GET", f"/api/v1/runs/{run['run_id']}/report")
    assert status == 200 and report["report"]["conclusive"] and report["report"]["comparison"]["fault"]["attempts"] >= 3
    status, md = client.call("GET", f"/api/v1/runs/{run['run_id']}/report.md")
    assert status == 200 and md.startswith(f"# Run report: {run['run_id']}")
    status, ev = client.call("GET", f"/api/v1/runs/{run['run_id']}/evidence")
    assert {"events.jsonl", "report.json", "manifest.json"} <= set(ev["files"])
    status, lines = client.call("GET", f"/api/v1/runs/{run['run_id']}/evidence/events.jsonl")
    assert status == 200 and len(lines.splitlines()) == final["last_seq"]
    status, runs = client.call("GET", "/api/v1/runs")
    assert [r["run_id"] for r in runs["runs"]] == [run["run_id"]]


def test_duplicate_and_concurrent_requests_cannot_double_mutate(setup):
    """Duplicate or concurrent run requests never apply a fault twice."""
    client, *_ = setup
    body = {"plan": plan_body(duration=20), "live": True}
    status, first = client.call("POST", "/api/v1/runs", body)
    assert status == 202
    status, dup = client.call("POST", "/api/v1/runs", body)
    assert status == 409 and dup["error"] == "DUPLICATE_REQUEST" and dup["run_id"] == first["run_id"]
    status, busy = client.call("POST", "/api/v1/runs", {"plan": plan_body(), "live": True})
    assert status == 409 and busy["error"] == "RUN_IN_PROGRESS" and busy["run_id"] == first["run_id"]
    wait_running(client, first["run_id"])
    assert client.call("POST", f"/api/v1/runs/{first['run_id']}/abort")[0] == 202
    final = wait_terminal(client, first["run_id"])
    assert (final["state"], final["stop_reason"]) == ("ABORTED", "manual_abort")
    status, again = client.call("POST", f"/api/v1/runs/{first['run_id']}/abort")
    assert status == 409 and again["error"] == "NOT_ACTIVE"
    assert len(RunStore(setup[1].db_path).recent_runs()) == 1


def test_gates_block_without_creating_a_run(setup):
    """A failing gate blocks a live run without creating a run record."""
    client, api, flag, options = setup
    options["gate_blockers"] = ["baseline: baseline is stale"]
    status, out = client.call("POST", "/api/v1/runs", {"plan": plan_body(), "live": True})
    assert status == 422 and out["error"] == "LIVE_EXECUTION_BLOCKED" and out["blockers"]
    assert RunStore(api.db_path).recent_runs() == [] and flag.read_bytes() == FLAG_FILE.read_bytes()


def test_unknown_run_and_evidence_paths_are_contained(setup):
    """Unknown runs give 404 and evidence paths cannot escape the results directory."""
    client, *_ = setup
    assert client.call("GET", "/api/v1/runs/run-nope")[0] == 404
    status, run = client.call("POST", "/api/v1/runs", {"plan": plan_body(), "live": True})
    wait_terminal(client, run["run_id"])
    for name in ("secrets", "..%2F..%2Fconfig%2Fsandbox.yaml", "../run.json"):
        assert client.call("GET", f"/api/v1/runs/{run['run_id']}/evidence/{name}")[0] == 404


# ----------------------------------------------------------- access rules
def test_access_controls(setup):
    """Wrong Host or Origin headers and oversized bodies are rejected."""
    client, api, *_ = setup
    assert client.raw("GET", "/api/v1/health", headers={"Host": "evil.example:80"})[0] == 403
    status, body, _ = client.raw("POST", "/api/v1/runs", json.dumps({"plan": plan_body(), "live": True}).encode(),
                                 {"Content-Type": "application/json", "Origin": "http://evil.example"})
    assert status == 403 and json.loads(body)["error"] == "ORIGIN_REJECTED"
    status, _, _ = client.raw("POST", "/api/v1/runs", b"plan=x", {"Content-Type": "application/x-www-form-urlencoded"})
    assert status == 415
    assert client.raw("POST", "/api/v1/plans", b"x" * 70000, {"Content-Type": "application/json"})[0] == 413
    assert client.raw("POST", "/api/v1/plans", b"{not json", {"Content-Type": "application/json"})[0] == 400
    assert client.raw("OPTIONS", "/api/v1/runs", headers={"Origin": "http://evil.example"})[0] == 403
    status, _, headers = client.raw("OPTIONS", "/api/v1/runs", headers={"Origin": "http://localhost:5173"})
    assert status == 204 and headers["Access-Control-Allow-Origin"] == "http://localhost:5173"
    assert RunStore(api.db_path).recent_runs() == []
    with pytest.raises(ValueError, match="loopback"):
        serve(api, host="0.0.0.0", port=0)


# ------------------------------------------------------------------ P3-03
def test_stream_delivers_ordered_events_and_resumes(setup):
    """The event stream delivers events in order and can resume after a sequence number."""
    client, *_ = setup
    status, run = client.call("POST", "/api/v1/runs", {"plan": plan_body(), "live": True})
    events, end = client.stream(run["run_id"])
    seqs = [seq for seq, _, _ in events]
    assert seqs == list(range(1, len(seqs) + 1))                           # ordered, no gaps
    assert end["state"] == "RECOVERED" and end["cleanup_status"] == "verified"
    resumed, end2 = client.stream(run["run_id"], last_event_id=5)
    assert [s for s, _, _ in resumed] == seqs[5:] and end2["state"] == "RECOVERED"
    status, page = client.call("GET", f"/api/v1/runs/{run['run_id']}/events?after_seq=5")
    assert [e["seq"] for e in page["events"]] == seqs[5:]


def test_disconnecting_the_stream_does_not_cancel_the_run(setup):
    """Closing the event stream does not cancel the run."""
    client, _, flag, _ = setup
    status, run = client.call("POST", "/api/v1/runs", {"plan": plan_body(duration=2), "live": True})
    partial, end = client.stream(run["run_id"], stop_after=3)
    assert len(partial) == 3 and end is None
    status, mid = client.call("GET", f"/api/v1/runs/{run['run_id']}")
    assert mid["cleanup_status"] in ("not_started", "pending")              # pending cleanup is visible
    final = wait_terminal(client, run["run_id"])
    assert final["state"] == "RECOVERED" and flag.read_bytes() == FLAG_FILE.read_bytes()
    resumed, end = client.stream(run["run_id"], last_event_id=partial[-1][0])
    assert resumed[0][0] == partial[-1][0] + 1 and end["state"] == "RECOVERED"


# ------------------------------------------------------- baseline capture
def test_baseline_capture_job(setup, tmp_path):
    """Baseline capture runs as a single background job with visible status."""
    client, api, *_ = setup
    gate = threading.Event()

    def fake_capture():
        """A fake baseline capture that waits for the test's signal."""
        gate.wait(5)
        now = datetime.now(timezone.utc)
        stats = [ProbeStats(probe=p, attempts=1, failures=0, failure_rate=0, p95_ms=1.0)
                 for p in ("POST /api/checkout", "GET /", "GET /api/products", "POST /api/cart", "GET /api/cart")]
        b = Baseline(baseline_id="baseline-job", started_at=now, finished_at=now,
                     sandbox=SandboxRef(docker_context="d", daemon_id="", compose_project="x"),
                     upstream_revision="x", flag_sha256="0" * 64, workload={}, probes=stats, controls=[],
                     telemetry={}, resources={}, health={}, criteria={}, healthy=True, blocking=[],
                     observations_file="x", limitations=[])
        path = api.baselines_dir / "baseline-job.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(b.model_dump_json())
        return b, path
    api.baseline_runner = fake_capture
    assert client.call("POST", "/api/v1/baselines", {})[0] == 202
    status, again = client.call("POST", "/api/v1/baselines", {})
    assert status == 409 and again["error"] == "JOB_IN_PROGRESS"
    gate.set()
    deadline = time.monotonic() + 5
    while client.call("GET", "/api/v1/baselines")[1]["job"]["state"] == "running" and time.monotonic() < deadline:
        time.sleep(0.05)
    status, listing = client.call("GET", "/api/v1/baselines")
    assert listing["job"]["state"] == "succeeded"
    assert [b["baseline_id"] for b in listing["baselines"]] == ["baseline-job"]
