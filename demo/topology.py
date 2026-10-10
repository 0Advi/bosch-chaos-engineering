"""Dependency graph (static map + live Jaeger edges) and blast-radius analysis."""
from __future__ import annotations

import json
from pathlib import Path
import statistics
import time
from urllib.request import urlopen

from controller.observer.endpoints import jaeger_api

from .catalog import ENDPOINTS, LAYOUT, PLATFORM

ROOT = Path(__file__).resolve().parents[1]
STATIC_MAP = ROOT / "artifacts" / "dependency_map.static.json"


def graph(lookback_min: int = 60) -> dict:
    """Nodes for all 25 services plus 'users'; application edges with observed call counts."""
    doc = json.loads(STATIC_MAP.read_text())
    nodes = [{"id": n["id"], "platform": n["id"] in PLATFORM, "xy": LAYOUT.get(n["id"], (0, 0))}
             for n in doc["nodes"]]
    nodes.append({"id": "users", "platform": False, "xy": LAYOUT["users"]})
    edges = {(e["from"], e["to"]): {"from": e["from"], "to": e["to"], "static": True, "calls": 0}
             for e in doc["edges"] if e["relationship"] == "application_call"}
    edges[("users", "frontend-proxy")] = {"from": "users", "to": "frontend-proxy", "static": False, "calls": 0}
    for dep in observed_dependencies(lookback_min):
        key = (dep["parent"], dep["child"])
        if dep["parent"] == dep["child"] or dep["child"] in PLATFORM and dep["child"] != "flagd":
            continue
        if dep["child"] == "flagd":
            continue  # every service polls flagd; drawing it adds noise, not insight
        edge = edges.setdefault(key, {"from": key[0], "to": key[1], "static": False, "calls": 0})
        edge["calls"] = dep["callCount"]
        edge["observed"] = True
    return {"nodes": nodes, "edges": list(edges.values()), "endpoints": ENDPOINTS}


def observed_dependencies(lookback_min: int) -> list[dict]:
    url = f"{jaeger_api()}/dependencies?endTs={int(time.time() * 1000)}&lookback={lookback_min * 60000}"
    try:
        with urlopen(url, timeout=5) as resp:
            return json.loads(resp.read()).get("data") or []
    except Exception:
        return []


def upstream_of(target: str, edges: list[dict]) -> set[str]:
    """Every service that (transitively) calls `target`."""
    callers, frontier = set(), {target}
    while frontier:
        nxt = {e["from"] for e in edges if e["to"] in frontier} - callers - {target}
        callers |= nxt
        frontier = nxt
    return callers


def criticality(edges: list[dict]) -> dict[str, dict]:
    """Per service: user endpoints that depend on it and its upstream fan-in."""
    out = {}
    services = {e["from"] for e in edges} | {e["to"] for e in edges}
    for svc in services - {"users"}:
        out[svc] = {"endpoints": [k for k, v in ENDPOINTS.items() if svc in v["services"]],
                    "callers": sorted(upstream_of(svc, edges) - {"users"})}
    return out


# --------------------------------------------------------------- per-run stats
def _p(values: list[float], q: float):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 1)


def endpoint_stats(records: list[dict], phase: str) -> dict:
    out = {}
    for endpoint in ENDPOINTS:
        rows = [r for r in records if r["phase"] == phase and r["endpoint"] == endpoint]
        lat = [r["latency_ms"] for r in rows if r["latency_ms"] is not None]
        ok = sum(1 for r in rows if r["ok"])
        statuses: dict[str, int] = {}
        for r in rows:
            if not r["ok"]:
                key = "timeout" if r["timeout"] else str(r["status"] or r["error"])
                if r.get("error_code"):
                    key += f" {r['error_code']}"
                statuses[key] = statuses.get(key, 0) + 1
        out[endpoint] = {"n": len(rows), "ok": ok,
                         "success": round(100 * ok / len(rows), 1) if rows else None,
                         "p50": round(statistics.median(lat), 1) if lat else None,
                         "p95": _p(lat, 0.95), "errors": statuses}
    return out


def impacted_endpoints(base: dict, fault: dict) -> dict[str, list[str]]:
    """Endpoints whose success dropped >=10 points, or whose p95 doubled (+300 ms)."""
    impacted = {}
    for endpoint, f in fault.items():
        b = base.get(endpoint) or {}
        if not f["n"] or not b.get("n"):
            continue
        reasons = []
        if b["success"] - f["success"] >= 10 and f["n"] - f["ok"] >= 2:
            reasons.append(f"success {b['success']}% -> {f['success']}%")
        if f["p95"] and b.get("p95") and f["p95"] > 2 * b["p95"] + 300:
            reasons.append(f"p95 {b['p95']} -> {f['p95']} ms")
        if reasons:
            impacted[endpoint] = reasons
    return impacted


def blast_radius(target: str, edges: list[dict], base: dict, fault: dict, span_errors: dict) -> dict:
    expected_eps = sorted(k for k, v in ENDPOINTS.items() if target in v["services"])
    impacted = impacted_endpoints(base, fault)
    unexpected = sorted(set(impacted) - set(expected_eps))
    callers = upstream_of(target, edges) - {"users"}
    erroring = sorted(s for s, v in span_errors.items() if v.get("errors", 0) > 0 and s != target)
    services = {target: "injected"}
    for svc in erroring:
        services[svc] = "errors"
    for ep in impacted:
        for svc in ENDPOINTS[ep]["services"]:
            if svc in ("frontend", "frontend-proxy") and svc not in services:
                services[svc] = "degraded"
    return {"target": target, "expected_endpoints": expected_eps, "impacted_endpoints": impacted,
            "unexpected_endpoints": unexpected,
            "healthy_endpoints": sorted(set(ENDPOINTS) - set(impacted)),
            "potential_services": sorted(callers), "services": services,
            "contained": not unexpected}
