"""Reversible fault adapters with a crash-safe journal.

Each adapter records how to undo itself in artifacts/demo-runs/.active-faults.json
*before* changing anything, so `restore_all()` (called on finish, abort, exit,
signals and the next server start) can always put the sandbox back.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import threading
import time
from uuid import uuid4

from controller.executor.flag_store import FLAG_FILE
from controller.observer.endpoints import app_url

from .catalog import CONTAINER_TARGETS, SCENARIOS

ROOT = Path(__file__).resolve().parents[1]
JOURNAL = ROOT / "artifacts" / "demo-runs" / ".active-faults.json"
PROJECT = "bosch-chaos-sandbox"
NETWORK = "bosch-chaos-sandbox"
_lock = threading.Lock()


# ------------------------------------------------------------------ journal
def _journal_read() -> list[dict]:
    try:
        return json.loads(JOURNAL.read_text())
    except (OSError, ValueError):
        return []


def _journal_write(entries: list[dict]):
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    if entries:
        JOURNAL.write_text(json.dumps(entries, indent=1))
    else:
        JOURNAL.unlink(missing_ok=True)


def _journal_add(entry: dict):
    with _lock:
        _journal_write(_journal_read() + [entry])


def _journal_remove(entry_id: str):
    with _lock:
        _journal_write([e for e in _journal_read() if e["id"] != entry_id])


def active_faults() -> list[dict]:
    return _journal_read()


# ------------------------------------------------------------------- docker
def docker(*args: str, timeout: float = 30, check: bool = True) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args[:2])} failed: {result.stderr.strip()[:300]}")
    return result.stdout.strip()


def container(service: str) -> str:
    ids = docker("ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}",
                 "--filter", f"label=com.docker.compose.service={service}").split()
    if len(ids) != 1:
        raise RuntimeError(f"expected one {service} container, found {len(ids)}")
    return ids[0]


def container_state(service: str) -> dict:
    try:
        doc = json.loads(docker("inspect", container(service)))[0]
    except Exception as error:
        return {"service": service, "error": str(error)}
    state = doc["State"]
    return {"service": service, "status": state.get("Status"), "running": state.get("Running"),
            "paused": state.get("Paused"), "oom_killed": state.get("OOMKilled"),
            "exit_code": state.get("ExitCode"), "restart_count": doc.get("RestartCount"),
            "health": (state.get("Health") or {}).get("Status"),
            "started_at": state.get("StartedAt"),
            "memory_limit_mb": round((doc["HostConfig"].get("Memory") or 0) / 2**20) or None,
            "networks": list((doc["NetworkSettings"].get("Networks") or {}).keys())}


def _write_flags(text: str):
    """Overwrite the flag file in place. A rename would make flagd's watcher see the file
    vanish (on Docker Desktop bind mounts it then stops following updates)."""
    with open(FLAG_FILE, "r+", encoding="utf-8", newline="") as handle:
        handle.write(text)
        handle.truncate()
        handle.flush()
        os.fsync(handle.fileno())


def live_flags() -> dict | None:
    """Variants flagd is actually serving (not just what the file says), via the proxy route."""
    from urllib.request import Request, urlopen
    body = json.dumps({"context": {"product_id": "OLJCESPC7Z"}}).encode()
    try:
        req = Request(app_url() + "/flagservice/flagd.evaluation.v1.Service/ResolveAll", data=body,
                      headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(req, timeout=5) as resp:
            flags = json.loads(resp.read())["flags"]
        return {k: v.get("variant") for k, v in flags.items()}
    except Exception:
        return None


def _served(flag: str) -> str | None:
    return (live_flags() or {}).get(flag)


def _wait_served(flag: str, variant: str, limit_s: float = 10) -> bool:
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        if _served(flag) == variant:
            return True
        time.sleep(0.5)
    return False


# ----------------------------------------------------------------- adapters
def _apply_flag(flag: str, variant: str) -> dict:
    doc = json.loads(FLAG_FILE.read_text())
    entry = doc["flags"][flag]
    if variant not in entry["variants"]:
        raise ValueError(f"{flag} has no variant {variant}")
    prior = json.loads(json.dumps(entry))
    if entry.get("targeting"):
        # productCatalogFailure: the upstream rule returns "off" for its one product.
        rule = entry["targeting"]["if"]
        entry["targeting"] = {"if": [rule[0], variant, "off"]}
    else:
        entry["defaultVariant"] = variant
    entry["state"] = "ENABLED"
    undo = {"id": uuid4().hex, "kind": "flag", "flag": flag, "prior_entry": prior}
    _journal_add(undo)
    text = json.dumps(doc, indent=2) + "\n"
    _write_flags(text)
    if not _wait_served(flag, variant):
        _write_flags(text)
        if not _wait_served(flag, variant):
            raise RuntimeError(f"flagd did not start serving {flag}={variant}; not measuring")
    return undo


def _undo_flag(undo: dict) -> str:
    doc = json.loads(FLAG_FILE.read_text())
    doc["flags"][undo["flag"]] = undo["prior_entry"]
    text = json.dumps(doc, indent=2) + "\n"
    _write_flags(text)
    prior = undo["prior_entry"]["defaultVariant"]
    if _wait_served(undo["flag"], prior):
        return f"{undo['flag']} restored (verified in flagd)"
    _write_flags(text)
    if _wait_served(undo["flag"], prior):
        return f"{undo['flag']} restored (verified in flagd after rewrite)"
    # Self-heal: flagd lost its file watch; a restart reloads the restored file.
    docker("restart", container("flagd"), timeout=60)
    if _wait_served(undo["flag"], prior, 30):
        return f"{undo['flag']} restored (flagd restarted to reload)"
    raise RuntimeError(f"flagd still serves {undo['flag']}={_served(undo['flag'])}")


def _container_fault(kind: str, service: str, variant: str) -> dict:
    if service not in CONTAINER_TARGETS:
        raise PermissionError(f"{service} is not an allowed fault target")
    cid = container(service)
    undo = {"id": uuid4().hex, "kind": kind, "service": service, "container": cid}
    if kind == "kill":
        _journal_add(undo)
        docker("kill", cid)
    elif kind == "pause":
        _journal_add(undo)
        docker("pause", cid)
    elif kind == "partition":
        info = json.loads(docker("inspect", cid))[0]["NetworkSettings"]["Networks"][NETWORK]
        undo["aliases"] = [a for a in (info.get("Aliases") or []) if a] or [service]
        _journal_add(undo)
        docker("network", "disconnect", NETWORK, cid)
    elif kind == "cpu":
        host = json.loads(docker("inspect", cid))[0]["HostConfig"]
        undo["prior_quota"] = host.get("CpuQuota") or -1
        undo["prior_period"] = host.get("CpuPeriod") or 100000
        _journal_add(undo)
        share = float(variant.rstrip("%")) / 100
        docker("update", "--cpu-period", "100000", "--cpu-quota", str(int(100000 * share)), cid)
    else:
        raise ValueError(kind)
    return undo


def _undo_container(undo: dict) -> str:
    cid = undo["container"]
    if undo["kind"] == "kill":
        docker("start", cid, timeout=60)
    elif undo["kind"] == "pause":
        if json.loads(docker("inspect", cid))[0]["State"].get("Paused"):
            docker("unpause", cid)
    elif undo["kind"] == "partition":
        networks = json.loads(docker("inspect", cid))[0]["NetworkSettings"]["Networks"] or {}
        if NETWORK not in networks:
            args = ["network", "connect"]
            for alias in undo["aliases"]:
                args += ["--alias", alias]
            docker(*args, NETWORK, cid)
    elif undo["kind"] == "cpu":
        quota = undo["prior_quota"] if undo["prior_quota"] > 0 else -1
        docker("update", "--cpu-period", str(undo["prior_period"]), "--cpu-quota", str(quota), cid)
    return f"{undo['service']} {undo['kind']} undone"


DB_MARK = "chaos_demo_overload"


def _apply_db_overload(variant: str) -> dict:
    cid = container("astronomy-db")
    count = int(variant.split()[0])
    undo = {"id": uuid4().hex, "kind": "db_overload", "service": "astronomy-db", "container": cid}
    _journal_add(undo)
    # Pure CPU loop: no rows are materialised, so it cannot fill memory or disk.
    query = (f"/* {DB_MARK} */ DO $$ DECLARE x float8 := 0; BEGIN "
             "FOR i IN 1..2000000000 LOOP x := x + sqrt(i); END LOOP; END $$;")
    for _ in range(count):
        docker("exec", "-d", cid, "psql", "-U", "postgres", "-c", query)
    return undo


def _undo_db_overload(undo: dict) -> str:
    docker("exec", undo["container"], "psql", "-U", "postgres", "-tAc",
           f"SELECT count(pg_cancel_backend(pid)) FROM pg_stat_activity "
           f"WHERE query LIKE '%{DB_MARK}%' AND pid <> pg_backend_pid()")
    return "heavy queries cancelled"


class TrafficSpike:
    """Concurrent synthetic users hammering browse paths until stopped."""

    def __init__(self, users: int):
        from urllib.request import urlopen
        self.users, self.stop, self.sent, self.urlopen = users, threading.Event(), 0, urlopen
        self.threads: list[threading.Thread] = []

    def _loop(self):
        paths = ["/", "/api/products?currencyCode=USD", "/api/recommendations?productIds=66VCHSJNUP&currencyCode=USD",
                 "/api/data?contextKeys=telescopes"]
        i = 0
        while not self.stop.is_set():
            try:
                with self.urlopen(app_url() + paths[i % len(paths)], timeout=10) as resp:
                    resp.read()
            except Exception:
                pass
            self.sent += 1
            i += 1

    def start(self):
        self.threads = [threading.Thread(target=self._loop, daemon=True) for _ in range(self.users)]
        for t in self.threads:
            t.start()

    def halt(self):
        self.stop.set()
        for t in self.threads:
            t.join(timeout=12)


# ------------------------------------------------------------------ facade
class Injection:
    """Apply one scenario (possibly composite); `restore()` undoes everything in reverse."""

    def __init__(self, scenario: str, target: str, variant: str):
        spec = SCENARIOS[scenario]
        if variant not in spec["variants"]:
            raise ValueError(f"{scenario} has no intensity {variant}")
        if "targets" in spec and target not in spec["targets"]:
            raise PermissionError(f"{target} is not an allowed target for {scenario}")
        self.spec, self.scenario, self.target, self.variant = spec, scenario, target, variant
        self.undos: list[dict] = []
        self.spike: TrafficSpike | None = None
        self.description = ""

    def apply(self):
        kind = self.spec["kind"]
        if kind == "flag":
            self.undos.append(_apply_flag(self.spec["flag"], self.variant))
            self.description = f"flagd: {self.spec['flag']} = {self.variant}"
        elif kind in ("kill", "pause", "partition", "cpu"):
            self.undos.append(_container_fault(kind, self.target, self.variant))
            verb = {"kill": "docker kill", "pause": "docker pause", "partition": "network disconnect",
                    "cpu": f"CPU quota {self.variant}"}[kind]
            self.description = f"{verb} {self.target}"
        elif kind == "db_overload":
            self.undos.append(_apply_db_overload(self.variant))
            self.description = f"{self.variant} heavy SQL on astronomy-db"
        elif kind in ("traffic", "combined"):
            if kind == "combined":
                self.undos.append(_container_fault("kill", self.target, "kill"))
            self.spike = TrafficSpike(int(self.variant.split()[0]))
            self.spike.start()
            self.description = f"{self.variant} concurrent users" + \
                (f" + docker kill {self.target}" if kind == "combined" else "")
        else:
            raise ValueError(kind)

    def restore(self) -> list[str]:
        results = []
        if self.spike:
            self.spike.halt()
            results.append(f"traffic stopped after {self.spike.sent} requests")
        for undo in reversed(self.undos):
            results.append(undo_entry(undo))
        self.undos = []
        return results


def undo_entry(undo: dict) -> str:
    try:
        if undo["kind"] == "flag":
            result = _undo_flag(undo)
        elif undo["kind"] == "db_overload":
            result = _undo_db_overload(undo)
        else:
            result = _undo_container(undo)
    except Exception as error:
        return f"FAILED to undo {undo['kind']}: {error}"
    _journal_remove(undo["id"])
    return result


def restore_all() -> list[str]:
    return [undo_entry(e) for e in reversed(_journal_read())]


def flags_clean() -> bool:
    """Clean only if both the file and flagd's live state have every flag off."""
    flags = json.loads(FLAG_FILE.read_text())["flags"]
    for entry in flags.values():
        if entry["defaultVariant"] != "off":
            return False
        rule = (entry.get("targeting") or {}).get("if")
        if rule and any(v != "off" for v in rule[1:]):
            return False
    served = live_flags()
    return served is not None and all(v == "off" for v in served.values())


def wait_running(service: str, limit_s: float = 60) -> bool:
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        state = container_state(service)
        if state.get("running") and not state.get("paused") and state.get("health") in (None, "healthy"):
            return True
        time.sleep(1)
    return False
