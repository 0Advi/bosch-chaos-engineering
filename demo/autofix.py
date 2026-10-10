"""Auto-fix: deploy a validated, operator-approved code fix into the sandbox and roll it back.

Interpreted services (payment, recommendation, email) are hot-patched: the
patched file is bind-mounted over the image's copy through a generated Compose
overlay, and only that service is recreated. Compiled services get the patch
saved for an image rebuild; they are never changed live.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

from . import faults
from .agent import validate_fix
from .catalog import HOT_PATCH

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_SRC = ROOT / "vendor" / "opentelemetry-demo" / "src"
AUTOFIX_DIR = ROOT / "deploy" / "autofix"
OVERLAY = AUTOFIX_DIR / "compose.autofix.yaml"
STATE = AUTOFIX_DIR / "active.json"
RUNS_DIR = ROOT / "artifacts" / "demo-runs"


def _compose() -> list[str]:
    sys.path.insert(0, str(ROOT / "scripts"))
    import demo as demo_script  # scripts/demo.py: pinned-revision checks + base compose command
    command = demo_script.compose()
    if OVERLAY.exists():
        command += ["-f", str(OVERLAY)]
    return command


def active() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def _write_state(state: dict):
    AUTOFIX_DIR.mkdir(parents=True, exist_ok=True)
    services: dict[str, dict] = {}
    for entry in state.values():
        mount = f"{AUTOFIX_DIR / entry['file']}:{HOT_PATCH[entry['file']]}:ro"
        services.setdefault(entry["service"], {"volumes": []})["volumes"].append(mount)
    if state:
        STATE.write_text(json.dumps(state, indent=1))
        OVERLAY.write_text(json.dumps({"services": services}, indent=1))  # JSON is valid YAML
    else:
        STATE.unlink(missing_ok=True)
        OVERLAY.unlink(missing_ok=True)


def _recreate(service: str) -> str:
    result = subprocess.run(_compose() + ["up", "-d", "--no-deps", "--force-recreate", "--wait",
                                          "--wait-timeout", "120", service],
                            capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        raise RuntimeError(f"compose recreate of {service} failed: {result.stderr[-400:]}")
    return result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "recreated"


def save_patch(run_id: str, fix: dict) -> Path | None:
    diff = (fix.get("validation") or {}).get("diff")
    if not diff:
        return None
    path = RUNS_DIR / f"{run_id}.fix.patch"
    path.write_text(diff)
    return path


def apply(run_id: str, fix: dict) -> dict:
    if faults.active_faults():
        raise RuntimeError("a fault is still active; restore it before deploying a fix")
    result = validate_fix(fix)  # re-validate against the pinned source right before deploying
    if not result["ok"]:
        raise RuntimeError(f"fix failed validation: {result['error']}")
    rel = result["file"]
    if rel not in HOT_PATCH:
        save_patch(run_id, {**fix, "validation": result})
        raise RuntimeError(f"{rel} belongs to a compiled service; the patch was saved to "
                           f"artifacts/demo-runs/{run_id}.fix.patch for an image rebuild (make build)")
    patched = (UPSTREAM_SRC / rel).read_text().replace(fix["find"], fix["replace"], 1)
    target = AUTOFIX_DIR / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(patched)
    service = rel.split("/")[0]
    state = active()
    state[rel] = {"file": rel, "service": service, "run_id": run_id, "title": fix.get("title"),
                  "applied_at": datetime.now(timezone.utc).isoformat()}
    _write_state(state)
    save_patch(run_id, {**fix, "validation": result})
    try:
        message = _recreate(service)
    except Exception:
        rollback(service)
        raise
    if not faults.wait_running(service, 90):
        rollback(service)
        raise RuntimeError(f"{service} did not become healthy with the fix; rolled back")
    return {"service": service, "file": rel, "deployed": True, "message": message, "diff": result["diff"]}


def rollback(service: str) -> dict:
    state = {k: v for k, v in active().items() if v["service"] != service}
    _write_state(state)
    for rel in [r for r in HOT_PATCH if r.startswith(service + "/")]:
        (AUTOFIX_DIR / rel).unlink(missing_ok=True)
    message = _recreate(service)
    return {"service": service, "rolled_back": True, "message": message}


def compare(before: dict, after: dict) -> dict:
    def pick(run):
        r = run.get("resilience") or {}
        facts = r.get("facts") or {}
        return {"score": r.get("score"), "grade": r.get("grade"), "parts": r.get("parts"),
                "fault_success": (run.get("totals") or {}).get("fault", {}).get("success"),
                "misleading_4xx": facts.get("misleading_4xx"), "timeouts": facts.get("timeouts"),
                "fault_failures": facts.get("fault_failures"), "time_to_recover_s": run.get("time_to_recover_s"),
                "impacted_paths": len((run.get("blast_radius") or {}).get("impacted_endpoints") or {}),
                "status_mix": _status_mix(run)}
    b, a = pick(before), pick(after)
    return {"before_run": before["run_id"], "after_run": after["run_id"], "before": b, "after": a,
            "score_delta": (a["score"] or 0) - (b["score"] or 0)}


def _status_mix(run: dict) -> dict:
    mix: dict[str, int] = {}
    for r in run.get("requests") or []:
        if r["phase"] == "fault" and not r["ok"]:
            key = "timeout" if r["timeout"] else str(r["status"])
            mix[key] = mix.get(key, 0) + 1
    return mix
