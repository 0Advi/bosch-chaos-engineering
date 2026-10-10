"""Export sanitized run evidence and rebuild reports from it (P3-01).

artifacts/results/<run_id>/
  run.json         run record (flag file texts replaced by their hashes)
  events.jsonl     every event in sequence order
  watchdog.log     watchdog output, if any
  report.json      RunReport built from the two files above
  report.md        the same report for people
  manifest.json    sha256 of every file
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from ..observer.fixtures import SECRETS
from ..observer.sanitize import assert_clean, sanitize
from ..schemas import Event, RunReport
from ..store import RunStore
from .build import ROOT, build_report
from .render import to_markdown

FILES = ("run.json", "events.jsonl", "watchdog.log", "report.json", "report.md", "manifest.json")
RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")


def _write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _run_record(run: dict) -> dict:
    keep = ("run_id", "plan_id", "request_id", "capability_id", "state", "stop_reason", "created_at",
            "fault_deadline_at", "hard_deadline_at", "restorer", "abort_requested", "last_seq",
            "plan", "result")
    record = {k: run.get(k) for k in keep}
    recovery = dict(run.get("recovery") or {})
    if recovery.get("change"):
        change = dict(recovery["change"])
        change.pop("prior_text", None)
        change.pop("applied_text", None)
        recovery["change"] = change
    record["recovery"] = recovery
    return sanitize(record)


def run_dir(results_dir: Path, run_id: str) -> Path:
    if not RUN_ID_RE.match(run_id):
        raise ValueError("invalid run id")
    return results_dir / run_id


def export_run(store: RunStore, run_id: str, results_dir: Path, root: Path = ROOT) -> Path:
    """Write (or refresh) the evidence directory for one run; returns its path."""
    out = run_dir(results_dir, run_id)
    out.mkdir(parents=True, exist_ok=True)
    record = _run_record(store.get(run_id))
    events = store.events(run_id)
    lines = [json.dumps(sanitize(e.model_dump(mode="json")), sort_keys=True) for e in events]
    assert_clean([record, lines], SECRETS)
    _write(out / "run.json", json.dumps(record, indent=2, sort_keys=True, default=str) + "\n")
    _write(out / "events.jsonl", "\n".join(lines) + "\n")
    log = store.path.parent / f"{run_id}.watchdog.log"
    if log.exists():
        text = "\n".join(sanitize(line) for line in log.read_text(errors="replace").splitlines())
        _write(out / "watchdog.log", text + "\n")
    report = report_from_dir(out, root)
    _write(out / "report.json", report.model_dump_json(indent=2) + "\n")
    _write(out / "report.md", to_markdown(report))
    manifest = {name: hashlib.sha256((out / name).read_bytes()).hexdigest()
                for name in FILES[:-1] if (out / name).exists()}
    _write(out / "manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return out


def load_dir(directory: Path) -> tuple[dict, list[Event]]:
    record = json.loads((directory / "run.json").read_text())
    events = [Event.model_validate_json(line) for line in
              (directory / "events.jsonl").read_text().splitlines() if line.strip()]
    return record, events


def report_from_dir(directory: Path, root: Path = ROOT) -> RunReport:
    """Rebuild the report from saved evidence only (no store, no model)."""
    record, events = load_dir(directory)
    return build_report(record, events, root)


def verify_manifest(directory: Path) -> list[str]:
    """Files whose content no longer matches the manifest."""
    manifest = json.loads((directory / "manifest.json").read_text())
    return sorted(name for name, digest in manifest.items()
                  if hashlib.sha256((directory / name).read_bytes()).hexdigest() != digest)
