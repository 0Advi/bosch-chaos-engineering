"""In-process stand-in for frontend-proxy, Jaeger and Prometheus (tests only).

Behaves like the pinned shop for the paths the observer uses: payment accepts
only the upstream VISA test card, failures return 422 PAYMENT_FAILED, the cart
is emptied only after a successful order, and each traceparent becomes a
Jaeger trace with frontend -> checkout -> payment spans.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

VALID_CARD = "4432-8015-6152-0454"


class FakeShop:
    """Minimal HTTP shop on a random loopback port: carts, checkout outcomes driven by test card
    numbers, and Jaeger-style traces.
    """
    def __init__(self):
        """Start with empty carts and traces and build the request handler and server."""
        self.carts: dict[str, list] = {}
        self.traces: dict[str, dict] = {}
        self.drop_traces = False
        self.metrics_missing = False
        self.metric_age = 12.0
        self.fail_every = 0          # every Nth checkout returns 500
        self.checkouts = 0
        self.lock = threading.Lock()
        shop = self

        class Handler(BaseHTTPRequestHandler):
            """Request handler for the fake shop and its Jaeger API."""
            def log_message(self, *args):
                """Keep test output quiet."""
                pass

            def _send(self, status, body=None, ctype="application/json"):
                """Send a JSON (or raw) response."""
                data = body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _trace_id(self):
                """Trace id from the traceparent header."""
                parts = (self.headers.get("traceparent") or "").split("-")
                return parts[1] if len(parts) == 4 else None

            def do_GET(self):
                """Serve the home page, products, cart reads and stored traces."""
                url = urlparse(self.path)
                q = parse_qs(url.query)
                if url.path == "/":
                    return self._send(200, b"<html>shop</html>", "text/html")
                if url.path == "/api/products":
                    return self._send(200, [{"id": "66VCHSJNUP"}])
                if url.path == "/api/cart":
                    user = q.get("sessionId", [""])[0]
                    return self._send(200, {"userId": user, "items": shop.carts.get(user, [])})
                if url.path.startswith("/jaeger/ui/api/traces/"):
                    tid = url.path.rsplit("/", 1)[1]
                    if shop.drop_traces or tid not in shop.traces:
                        return self._send(404, {"data": None, "errors": [{"code": 404}]})
                    return self._send(200, {"data": [shop.traces[tid]]})
                if url.path == "/api/v1/query":
                    result = [] if shop.metrics_missing else [{"metric": {}, "value": [0, str(shop.metric_age)]}]
                    return self._send(200, {"status": "success", "data": {"resultType": "vector", "result": result}})
                return self._send(404, {})

            def do_POST(self):
                """Handle add-to-cart and checkout."""
                url = urlparse(self.path)
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if url.path == "/api/cart":
                    shop.carts.setdefault(body["userId"], []).append(body["item"])
                    return self._send(200, {"userId": body["userId"], "items": shop.carts[body["userId"]]})
                if url.path == "/api/checkout":
                    return self._checkout(body)
                return self._send(404, {})

            def _checkout(self, body):
                """Simulate a checkout: decline, payment failure or success depending on the card,
                and record its trace.
                """
                with shop.lock:
                    shop.checkouts += 1
                    n = shop.checkouts
                card = body["creditCard"]
                ok_card = card["creditCardNumber"] == VALID_CARD and card["creditCardExpirationYear"] >= 2026
                tid = self._trace_id()
                if shop.fail_every and n % shop.fail_every == 0:
                    shop.record_trace(tid, payment_error=None, failed=True)
                    return self._send(500, {"error": "Failed to place order."})
                if not ok_card:
                    shop.record_trace(tid, payment_error="Credit card info is invalid.")
                    return self._send(422, {"error": "Your payment could not be processed.",
                                            "code": "PAYMENT_FAILED"})
                tx = str(uuid4())
                shop.record_trace(tid, transaction_id=tx)
                shop.carts.pop(body["userId"], None)
                return self._send(200, {"orderId": str(uuid4()), "items": []})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def record_trace(self, tid, payment_error=None, transaction_id=None, failed=False):
        """Store a Jaeger-style trace for a checkout, with error spans and logs when payment failed."""
        if not tid:
            return

        def span(sid, pid, op, error=False, logs=()):
            """Build one span with its tags and logs."""
            tags = [{"key": "otel.status_code", "value": "ERROR"}] if error else []
            if error and pid == "p3":
                tags.append({"key": "rpc.grpc.status_code", "value": 2})
            return {"traceID": tid, "spanID": sid, "operationName": op, "processID": pid,
                    "duration": 1500, "tags": tags, "logs": list(logs)}
        charged = [{"fields": [{"key": "demo.payment.transaction.id", "value": transaction_id}]}] \
            if transaction_id else []
        spans = [span("a1", "p1", "POST /api/checkout", error=failed or bool(payment_error)),
                 span("b2", "p2", "oteldemo.CheckoutService/PlaceOrder", error=failed or bool(payment_error),
                      logs=charged)]
        if not failed:
            spans.append(span("c3", "p3", "charge", error=bool(payment_error)))
        self.traces[tid] = {"traceID": tid, "spans": spans,
                            "processes": {"p1": {"serviceName": "frontend"}, "p2": {"serviceName": "checkout"},
                                          "p3": {"serviceName": "payment"}}}

    def __enter__(self):
        """Start serving in a background thread."""
        self.thread.start()
        return self

    def __exit__(self, *exc):
        """Stop the server."""
        self.server.shutdown()
        self.server.server_close()


class FakeRuntime:
    """Fake container runtime for baseline tests."""
    def __init__(self, restarts_after: int = 0):
        """Optionally report restarts after the first inventory call."""
        self.calls, self.restarts_after = 0, restarts_after

    def inventory(self):
        """Healthy running containers (with restarts on later calls if configured)."""
        self.calls += 1
        restarts = self.restarts_after if self.calls > 1 else 0
        return {s: {"id": s[:12], "status": "running", "health": "healthy",
                    "restarts": restarts if s == "payment" else 0, "oom_killed": False}
                for s in ("frontend", "checkout", "payment")}

    def stats(self):
        """Fixed resource stats for the payment container."""
        return {"payment": {"cpu_percent": 1.5, "mem_bytes": 50_000_000, "mem_limit_bytes": 120_000_000}}
