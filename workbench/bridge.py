"""Same-origin P5 adapter over P3 services; no independent fault executor.

Patch preparation uses one reviewed local candidate. HTTP cannot supply commands,
claim application tests passed, approve a deployment, or bypass runner gates.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

from controller.api.server import ApiError
from controller.baseline.check import usable_blockers
from controller.baseline.measure import load_criteria
from controller.catalog import Catalog, load_policy
from controller.executor.flag_store import FLAG_FILE
from controller.report.build import build_report
from controller.report.evidence import run_dir
from controller.safety.sandbox import load_spec
from controller.schemas import Plan, RunState, TERMINAL
from improvements.patches import prepare, structural_checks

ROOT = Path(__file__).resolve().parents[1]


class WorkbenchBridge:
    """Serves the workbench UI and its /api/workbench routes on top of the controller API."""
    assets = {"/": ("index.html", "text/html; charset=utf-8"),
              "/index.html": ("index.html", "text/html; charset=utf-8"),
              "/style.css": ("style.css", "text/css"),
              **{f"/{n}.mjs": (f"{n}.mjs", "text/javascript") for n in ("app", "api", "state")}}

    def __init__(self, api, *, preflight=None):
        """Wrap the controller API, with an optional preflight result already known."""
        self.api = api
        self.lock = threading.Lock()
        self.preflight = preflight
        self.preflight_at = time.monotonic() if preflight is not None else 0
        self.checking = False

    def _refresh_preflight(self):
        """Re-run the Docker sandbox inspection in the background."""
        try:
            self.preflight = self.api.preflight(None)
        except Exception:
            self.preflight = {"isolation_approved": False, "errors": ["Docker inspection unavailable; run make preflight."]}
        finally:
            self.preflight_at = time.monotonic()
            self.checking = False

    def readiness(self):
        """Whether live runs are allowed now: policy, sandbox spec, inspection result and active
        runs (inspection refreshed in the background).
        """
        # Docker inspection can take 30 seconds. Never block UI/abort requests on it.
        with self.lock:
            if not self.checking and time.monotonic() - self.preflight_at > 15:
                self.checking = True
                threading.Thread(target=self._refresh_preflight, daemon=True).start()
        policy, spec = load_policy(), load_spec()
        inspection = self.preflight or {"isolation_approved": False, "errors": ["Sandbox inspection pending."]}
        baselines = self.api.list_baselines(None)
        usable = []
        for row in baselines["baselines"]:
            path = (self.api.root / row["file"]).resolve()
            if not path.is_relative_to(self.api.baselines_dir.resolve()):
                continue
            if not usable_blockers(path, spec["daemon_id"], FLAG_FILE, 24, profile=load_criteria()["workload"],
                                   sandbox_identity=spec, upstream_revision="dedc0178918e260823323b8d95005a8cb924b007"):
                usable.append(row["file"])
        blockers = list(inspection.get("errors", []))
        if inspection.get("isolation_approved") is not True:
            blockers.append("Approve and verify a dedicated sandbox before injection.")
        if policy.get("execution_enabled") is not True:
            blockers.append("Execution policy is disabled.")
        if not usable:
            blockers.append("Capture two reproducible healthy baselines and derive the payment policy.")
        if not (self.api.root / "config/payment_policy.yaml").is_file():
            blockers.append("Derived payment policy is missing.")
        if self.api.store().active_runs():
            blockers.append("An unfinished run requires recovery before another experiment.")
        if baselines["job"] and baselines["job"].get("state") == "running":
            blockers.append("Baseline measurement is running.")
        return {"execution_enabled": policy.get("execution_enabled") is True,
                "isolation_approved": inspection.get("isolation_approved") is True,
                "baseline_healthy": bool(usable), "baseline_ref": usable[0] if usable else None,
                "watchdog_ready": inspection.get("isolation_approved") is True,
                "watchdog_note": "Independent process must acknowledge readiness before any fault is armed.",
                "live_capabilities": policy.get("live_capabilities", []),
                "sandbox": {k: spec[k] for k in ("docker_context", "daemon_id", "compose_project")},
                "blockers": list(dict.fromkeys(blockers)), "baseline_job": baselines["job"]}

    def view(self, record):
        """A run as the workbench shows it, with cleanup, recovery and links to its evidence."""
        events = self.api.store().events(record["run_id"])
        report = build_report(record, events, self.api.root)
        recovered = any(e.type == "state_changed" and e.data.get("recovered") is True for e in events)
        return {**self.api.run_view(record, events),
                "cleanup_verified": report.cleanup_status == "verified",
                "recovery_outcome": "verified" if recovered else "unverified",
                "evidence_refs": [f"/api/v1/runs/{record['run_id']}/report",
                                  f"/api/v1/runs/{record['run_id']}/evidence"]}

    def detail(self, run_id):
        """Full detail of a run: record, observations, timeline and report."""
        record = self.api._run(run_id)
        events = self.api.store().events(run_id)
        observations = [{"observation_id": f"event-{e.seq}", "probe": e.data.get("probe", e.data.get("kind", "observation")),
                         "phase": e.data.get("phase", e.state.value),
                         "outcome": "matched" if e.data.get("matched") is True else "unverified",
                         "http_status": e.data.get("http_status"), "latency_ms": e.data.get("latency_ms"),
                         "trace_id": e.data.get("trace_id"), "fresh": False, "complete": False}
                        for e in events if e.type == "observation"]
        dx_path = run_dir(self.api.results_dir, run_id) / "diagnosis.json"
        dx = json.loads(dx_path.read_text()) if dx_path.is_file() else None
        review_path = run_dir(self.api.results_dir, run_id) / "patch-review.json"
        review = json.loads(review_path.read_text()) if review_path.is_file() else None
        if review:
            try:
                from controller.schemas.contracts import Patch
                structural_checks(self.api.root / "artifacts/patch-workspaces" / review["patch"]["patch_id"],
                                  Patch.model_validate(review["patch"]))
            except (ValueError, OSError):
                review["tests"] = {"passed": False, "checks": [], "reason": "Prepared source changed or is unavailable."}
        return {"run": self.view(record), "observations": observations,
                "diagnosis": dx.get("diagnosis") if dx else None, "diagnosis_result": dx,
                "patch_review": review, "comparison": None}

    def dispatch(self, method, path, req):
        """Route one workbench request: static assets, snapshot, plan, run, abort, diagnose or
        patch-candidate calls.
        """
        if method == "GET" and path in self.assets:
            name, mime = self.assets[path]
            return 200, (ROOT / "workbench" / name).read_text(), mime
        suffix = path.removeprefix("/api/workbench")
        if method == "GET" and suffix == "":
            return 200, {"ok": True, "result": {"mode": "controller", "readiness": self.readiness(),
                        "capabilities": [c.model_dump(mode="json") for c in Catalog().by_id.values()],
                        "history": [self.view(r) for r in self.api.store().recent_runs(20)]}}
        if method == "GET" and suffix.startswith("/runs/"):
            parts = suffix.split("/")
            if len(parts) == 3:
                out = self.detail(parts[2])
            elif len(parts) == 4 and parts[3] == "events":
                after = int(req.query.get("after_seq", ["0"])[0])
                if after < 0:
                    raise ApiError("INVALID_INPUT", "negative event cursor")
                out = {"events": self.api.run_events(parts[2], after)}
            else:
                raise ApiError("NOT_FOUND", "unknown workbench route")
            return 200, {"ok": True, "result": out}
        if method != "POST":
            raise ApiError("NOT_FOUND", "unknown workbench route")
        body = req.json()
        if suffix == "/plans/validate":
            data = dict(body)
            baseline = data.pop("baseline_ref", None) or self.readiness()["baseline_ref"]
            proposed = self.api._tool("propose_plan", data)["result"]
            proposed.update(requested_by="user", baseline_ref=baseline)
            plan = Plan.model_validate(proposed)
            from controller.executor.runner import dry_run
            dry_run(plan)
            out = {"plan": plan.model_dump(mode="json")}
        elif suffix == "/baselines":
            out = self.api.start_baseline_job()
        elif suffix == "/runs":
            plan = self.api._plan(body.get("plan"))
            key = req.headers.get("Idempotency-Key")
            if not key or len(key) > 128:
                raise ApiError("INVALID_INPUT", "a bounded Idempotency-Key is required")
            plan = plan.model_copy(update={"request_id": "req-" + hashlib.sha256(key.encode()).hexdigest()[:40]})
            # Durable run request identity survives browser/server restarts.
            with self.lock:
                store = self.api.store()
                row = store.db.execute("SELECT run_id FROM runs WHERE request_id=?", (plan.request_id,)).fetchone()
                if row:
                    existing = store.get(row["run_id"])
                    if existing["plan"] != plan.model_dump(mode="json"):
                        raise ApiError("DUPLICATE_REQUEST", "idempotency key reused with a different plan")
                    out = self.api.run_summary(row["run_id"])
                else:
                    out = self.api.start_run(plan)
        elif suffix.startswith("/runs/"):
            parts = suffix.split("/")
            if len(parts) != 4:
                raise ApiError("NOT_FOUND", "unknown workbench route")
            run_id, action = parts[2:]
            if action == "abort":
                record = self.api._run(run_id)
                out = self.api.run_summary(run_id) if record["abort_requested"] else self.api.abort_run(run_id)
            elif action == "diagnosis":
                class Request:
                    """Minimal request object asking for the baseline diagnoser."""
                    def json(self):
                        """The request body."""
                        return {"diagnoser": "baseline"}
                _, out = self.api.diagnose(Request(), run_id)
            elif action == "patch":
                out = self.prepare_candidate(run_id)
            else:
                raise ApiError("NOT_FOUND", "unknown workbench action")
        elif suffix.startswith("/patches/"):
            raise ApiError("DEPLOYMENT_NOT_READY", "Application test receipts, immutable images, and a persistent Docker deployment operator are required. HTTP cannot assert these prerequisites.", status=409)
        else:
            raise ApiError("NOT_FOUND", "unknown workbench route")
        return 200, {"ok": True, "result": out}

    def prepare_candidate(self, run_id):
        """Write a patch-review file for a finished run (refused while restoration is still in
        progress).
        """
        record = self.api._run(run_id)
        if RunState(record["state"]) not in TERMINAL:
            raise ApiError("NOT_FINISHED", "finish restoration before preparing a candidate", status=409)
        with self.lock:
            directory = run_dir(self.api.results_dir, run_id)
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / "patch-review.json"
            if target.exists():
                return json.loads(target.read_text())
            data = (self.api.root / "patches/checkout-preserve-rpc-code.candidate.patch").read_bytes()
            patch_id = "patch-" + run_id.removeprefix("run-")
            workspace = self.api.root / "artifacts/patch-workspaces" / patch_id
            patch = prepare(self.api.root / "vendor/opentelemetry-demo", workspace, data, patch_id,
                            "dedc0178918e260823323b8d95005a8cb924b007",
                            "Provisional checkout RPC error preservation; requires reproduction, application tests and matched replay.")
            checks = structural_checks(workspace, patch)
            out = {"patch": patch.model_dump(mode="json"), "diff": data.decode(),
                   "tests": {"passed": False, "checks": ["source-integrity", "whitespace"], "structural": checks},
                   "approval": None, "deployment": None, "deployment_ready": False}
            target.write_text(json.dumps(out, indent=2) + "\n")
            return out
