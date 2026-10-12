"""Deterministic checks for AI-proposed code fixes (no LLM dependencies, unit-testable).

A fix is accepted only if:
  1. it targets an application source file of the pinned upstream checkout,
  2. its `find` text matches that file exactly once,
  3. the patched file passes a local syntax check where a checker exists, and
  4. it does not "fix" the weakness by deleting or disabling the fault-injection code itself
     (that would make the experiment pass without making the service more resilient).
"""
from __future__ import annotations

import difflib
import json
from pathlib import Path
import re
import subprocess
import tempfile

from . import evidence
from .catalog import HOT_PATCH

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = evidence.UPSTREAM
FLAG_FILE = ROOT / "deploy" / "flagd" / "demo.flagd.json"
GENERIC_FAULT_MARKERS = ("FEATURE_FLAG", "OpenFeature", "featureFlag", "flags.")


def _fault_tokens() -> set[str]:
    """Identifiers the upstream services use for their built-in faults: flag keys as written in
    flagd (paymentFailure), as Go identifiers (PaymentFailure) and as constants (PAYMENT_FAILURE)."""
    try:
        keys = json.loads(FLAG_FILE.read_text())["flags"].keys()
    except (OSError, ValueError, KeyError):
        keys = []
    tokens = set()
    for key in keys:
        tokens |= {key, key[0].upper() + key[1:], re.sub(r"(?<!^)(?=[A-Z])", "_", key).upper()}
    return tokens


def removes_fault_injection(original: str, patched: str) -> list[str]:
    """Lines of fault-injection code that the patch deletes or comments out."""
    tokens = _fault_tokens()
    kept = {line.strip() for line in patched.splitlines()}
    removed = []
    for line in original.splitlines():
        text = line.strip()
        if not text or text in kept:
            continue
        if any(t in text for t in tokens) or any(m in text for m in GENERIC_FAULT_MARKERS):
            removed.append(text)
    return removed


def syntax_check(rel: str, text: str) -> dict:
    """Syntax-check a patched file with the matching local tool (node --check, py_compile, ruby -c
    or gofmt); languages without a local checker pass with a note.
    """
    suffix = Path(rel).suffix
    commands = {".js": ["node", "--check"], ".py": ["python3", "-m", "py_compile"], ".rb": ["ruby", "-c"],
                ".go": ["gofmt", "-e", "-l"]}
    if suffix not in commands:
        return {"ok": True, "tool": None, "output": f"no local {suffix} compiler; not syntax-checked"}
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / Path(rel).name
        path.write_text(text)
        try:
            result = subprocess.run(commands[suffix] + [str(path)], capture_output=True, text=True, timeout=60)
        except FileNotFoundError:
            return {"ok": True, "tool": None, "output": f"{commands[suffix][0]} not installed; not syntax-checked"}
    output = (result.stdout + result.stderr).replace(str(path), rel).strip()
    return {"ok": result.returncode == 0, "tool": " ".join(commands[suffix][:2]), "output": output[:600] or "OK"}


def validate_fix(fix: dict) -> dict:
    """Accept or reject an AI fix: real application source file, unique find text, no deleting of
    the injected fault, and a passing syntax check; returns the diff and how it can be deployed.
    """
    rel = fix["file"].strip().lstrip("/").removeprefix("src/")
    service = rel.split("/")[0]
    try:
        if rel not in evidence.list_source(service):
            return {"ok": False, "error": f"{rel} is not an application source file of {service}"}
    except PermissionError as error:
        return {"ok": False, "error": str(error)}
    original = (UPSTREAM / rel).read_text()
    count = original.count(fix["find"])
    if count != 1 or not fix["find"].strip():
        return {"ok": False, "error": f"`find` must match the file exactly once (matched {count} times). "
                                       "Copy complete lines verbatim from read_source output, without line numbers."}
    patched = original.replace(fix["find"], fix["replace"], 1)
    removed = removes_fault_injection(original, patched)
    if removed:
        return {"ok": False, "error": "the fix deletes or disables the injected fault itself "
                                       f"({removed[0][:120]}). Keep the fault code unchanged and make the "
                                       "callers resilient instead: timeouts, retries with idempotency, "
                                       "fallbacks, circuit breakers or correct error mapping."}
    check = syntax_check(rel, patched)
    if not check["ok"]:
        return {"ok": False, "error": f"syntax check failed: {check['output']}"}
    diff = "".join(difflib.unified_diff(original.splitlines(True), patched.splitlines(True),
                                        f"a/src/{rel}", f"b/src/{rel}", n=3))
    return {"ok": True, "file": rel, "diff": diff, "syntax": check, "hot_patchable": rel in HOT_PATCH,
            "deploy": "hot-patch into running container" if rel in HOT_PATCH
            else "compiled service: needs an image rebuild (make build) - patch saved only"}
