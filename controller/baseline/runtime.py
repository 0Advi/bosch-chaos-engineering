"""Read-only Docker runtime facts for the sandbox project: health and resources."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UNITS = {"b": 1, "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3,
         "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3}


def _bytes(text: str):
    match = re.match(r"\s*([\d.]+)\s*([a-zA-Z]+)", text)
    if not match:
        return None
    return int(float(match.group(1)) * UNITS.get(match.group(2).lower(), 1))


def _docker(*args: str) -> str:
    return subprocess.run(["docker", *args], capture_output=True, text=True,
                          timeout=60, check=True).stdout


class DockerRuntime:
    def __init__(self, project: str):
        self.project = project

    def _ids(self) -> list[str]:
        return _docker("ps", "--all", "--quiet", "--no-trunc", "--filter",
                       f"label=com.docker.compose.project={self.project}").split()

    def inventory(self) -> dict:
        ids = self._ids()
        services = {}
        for c in json.loads(_docker("inspect", *ids)) if ids else []:
            state = c["State"]
            services[c["Config"]["Labels"]["com.docker.compose.service"]] = {
                "id": c["Id"][:12], "status": state["Status"],
                "health": (state.get("Health") or {}).get("Status"),
                "restarts": c["RestartCount"], "oom_killed": state["OOMKilled"]}
        return services

    def stats(self) -> dict:
        names = {v["id"]: k for k, v in self.inventory().items()}
        out = _docker("stats", "--no-stream", "--format", "{{json .}}", *names) if names else ""
        result = {}
        for line in out.splitlines():
            row = json.loads(line)
            used, _, limit = row.get("MemUsage", "").partition("/")
            service = names.get(row.get("ID", "")[:12], row.get("Name"))
            result[service] = {"cpu_percent": float(row.get("CPUPerc", "0%").rstrip("%") or 0),
                               "mem_bytes": _bytes(used), "mem_limit_bytes": _bytes(limit)}
        return result


def upstream_revision() -> str:
    try:
        return subprocess.run(["git", "-C", str(ROOT / "vendor" / "opentelemetry-demo"), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=10, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def health_findings(before: dict, after: dict, max_restarts: int) -> list[str]:
    findings = []
    for service, state in sorted(after.items()):
        if state["status"] != "running":
            findings.append(f"{service} is {state['status']}")
        elif state["health"] not in (None, "healthy"):
            findings.append(f"{service} health is {state['health']}")
        if state["oom_killed"]:
            findings.append(f"{service} was OOM-killed")
        restarted = state["restarts"] - before.get(service, state)["restarts"]
        if restarted > max_restarts:
            findings.append(f"{service} restarted {restarted}x during measurement")
    if missing := sorted(set(before) - set(after)):
        findings.append(f"services disappeared during measurement: {missing}")
    return findings
