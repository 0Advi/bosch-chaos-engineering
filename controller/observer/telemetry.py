"""Read-only telemetry adapters (P1-02): Jaeger traces, Prometheus, OpenSearch logs.

Every lookup reports what it could not find instead of guessing.
"""
from __future__ import annotations

import json
import subprocess
from typing import Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from .sanitize import sanitize

# Freshness probes. Span metrics come from the Collector's span_metrics connector;
# target_info is the resource metric scripts/verify_startup.py already relies on.
METRIC_FRESHNESS_QUERIES = {
    "payment_span_metrics": 'traces_span_metrics_calls_total{service_name="payment"}',
    "checkout_span_metrics": 'traces_span_metrics_calls_total{service_name="checkout"}',
    "payment_target_info": 'target_info{service_name="payment"}',
}


def _tags(items) -> dict:
    """Turn a Jaeger tag list into a dict."""
    return {t["key"]: t.get("value") for t in items or []}


def summarize_trace(trace: dict) -> dict:
    """Reduce a Jaeger trace to the service/edge/status facts the controller uses."""
    processes = {pid: p.get("serviceName") for pid, p in trace.get("processes", {}).items()}
    spans, charge_spans, transaction_ids = [], [], set()
    for span in trace.get("spans", []):
        tags = _tags(span.get("tags"))
        service = processes.get(span.get("processID"), "unknown")
        operation = span.get("operationName", "")
        error = tags.get("otel.status_code") == "ERROR" or tags.get("error") is True
        summary = {"span_id": span.get("spanID"), "service": service, "operation": operation,
                   "duration_ms": round(span.get("duration", 0) / 1000, 1), "error": error,
                   "rpc_code": tags.get("rpc.grpc.status_code"),
                   "http_status": tags.get("http.response.status_code", tags.get("http.status_code"))}
        if error:
            summary["status_message"] = sanitize(str(tags.get("otel.status_description", "")))[:200]
        spans.append(summary)
        if service == "payment" and "charge" in operation.lower():
            charge_spans.append(summary)
        for log in span.get("logs", []):
            tx = _tags(log.get("fields")).get("demo.payment.transaction.id")
            if tx:
                transaction_ids.add(tx)
        if tags.get("demo.payment.transaction.id"):
            transaction_ids.add(tags["demo.payment.transaction.id"])
    return {"services": sorted({s["service"] for s in spans}), "span_count": len(spans),
            "error_spans": [s for s in spans if s["error"]], "charge_spans": charge_spans,
            "transaction_ids": sorted(transaction_ids)}


class Telemetry:
    """Read-only access to Jaeger traces, Prometheus metrics and (optionally) indexed logs."""
    def __init__(self, jaeger_api: str, metrics_url: str, timeout: float = 5,
                 log_search: Optional[Callable[[str], Optional[dict]]] = None):
        """Remember the Jaeger and Prometheus URLs, timeout and optional log search."""
        self.jaeger_api, self.metrics_url, self.timeout = jaeger_api.rstrip("/"), metrics_url.rstrip("/"), timeout
        self.log_search = log_search

    def _get(self, url: str):
        """GET a URL and return (status, parsed JSON); network errors give (None, None)."""
        try:
            with urlopen(url, timeout=self.timeout) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except HTTPError as error:
            return error.code, None
        except (URLError, TimeoutError, OSError, ValueError):
            return None, None

    # ------------------------------------------------------------- traces
    def trace(self, trace_id: str) -> Optional[dict]:
        """Return a summary, or None when Jaeger has no such trace (yet)."""
        status, body = self._get(f"{self.jaeger_api}/traces/{trace_id}")
        if status != 200 or not body or not body.get("data"):
            return None
        return summarize_trace(body["data"][0])

    # ------------------------------------------------------------ metrics
    def query(self, promql: str) -> Optional[list]:
        """Run an instant PromQL query; None if Prometheus fails."""
        status, body = self._get(f"{self.metrics_url}/api/v1/query?" + urlencode({"query": promql}))
        if status != 200 or not body or body.get("status") != "success":
            return None
        return body["data"]["result"]

    def metric_age_seconds(self, selector: str) -> Optional[float]:
        """Seconds since a metric was last written (None if absent)."""
        result = self.query(f"time() - max(timestamp({selector}))")
        if not result:
            return None
        return round(float(result[0]["value"][1]), 1)

    def metric_freshness(self) -> dict:
        """Age of each freshness-probe metric (payment and checkout span metrics, payment target
        info).
        """
        return {name: {"query": q, "age_seconds": self.metric_age_seconds(q)}
                for name, q in METRIC_FRESHNESS_QUERIES.items()}

    # --------------------------------------------------------------- logs
    def logs_for_trace(self, trace_id: str) -> Optional[dict]:
        """{"hits": n, "excerpts": [...]}; None when logs could not be queried."""
        if not self.log_search:
            return None
        return self.log_search(trace_id)


def _pick(source: dict, *keys):
    """First non-empty value among several nested key paths in a log document."""
    for key in keys:
        node = source
        for part in key.split("/"):
            node = node.get(part) if isinstance(node, dict) else None
        if node not in (None, ""):
            return node
    return None


def opensearch_log_search(project: str) -> Callable[[str], Optional[dict]]:
    """Search indexed OTel logs by trace ID inside the sandbox OpenSearch container."""
    def search(trace_id: str) -> Optional[dict]:
        """Search OpenSearch (inside the sandbox) for log records mentioning a trace id and return
        sanitized excerpts.
        """
        try:
            ids = subprocess.run(
                ["docker", "ps", "--quiet", "--filter", f"label=com.docker.compose.project={project}",
                 "--filter", "label=com.docker.compose.service=opensearch"],
                capture_output=True, text=True, timeout=15, check=True).stdout.split()
            if len(ids) != 1:
                return None
            query = json.dumps({"size": 5, "query": {"query_string": {"query": f'"{trace_id}"'}}})
            out = subprocess.run(
                ["docker", "exec", ids[0], "curl", "-fsS", "-H", "Content-Type: application/json",
                 "http://localhost:9200/otel-logs*/_search", "-d", query],
                capture_output=True, text=True, timeout=20, check=True).stdout
            hits = json.loads(out)["hits"]
        except (OSError, subprocess.SubprocessError, ValueError, KeyError):
            return None
        total = hits["total"]["value"] if isinstance(hits.get("total"), dict) else hits.get("total", 0)
        excerpts = []
        for hit in hits.get("hits", []):
            src = hit.get("_source", {})
            excerpts.append(sanitize({
                "service": _pick(src, "resource/service.name", "resource.service.name", "serviceName"),
                "severity": _pick(src, "severityText", "severity/text", "severity.text"),
                "body": str(_pick(src, "body", "Body", "message") or "")[:300],
            }))
        return {"hits": total, "excerpts": excerpts}
    return search
