"""Synthetic HTTP client for the loopback frontend-proxy.

Every request carries synthetic baggage and a fresh W3C traceparent, so its
trace can be looked up in Jaeger afterwards.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import secrets
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

# Upstream demo test-only card and address (same as scripts/verify_startup.py).
FAKE_CARD = {"creditCardNumber": "4432-8015-6152-0454", "creditCardCvv": 672,
             "creditCardExpirationYear": 2039, "creditCardExpirationMonth": 1}
ADDRESS = {"streetAddress": "1600 Amphitheatre Parkway", "zipCode": "94043",
           "city": "Mountain View", "state": "CA", "country": "United States"}
SYNTHETIC_EMAIL = "chaos-synthetic@example.com"
LOOPBACK = ("http://127.0.0.1:", "http://localhost:")


class Shop:
    """Bounded client; refuses anything except the loopback sandbox proxy."""

    def __init__(self, app_url: str, timeout: float):
        """Create a client for the loopback shop only (anything else is refused)."""
        if not app_url.startswith(LOOPBACK):
            raise PermissionError("synthetic requests only target the loopback sandbox proxy")
        self.app_url, self.timeout = app_url.rstrip("/"), timeout

    def call(self, path: str, body: dict | None = None, method: str | None = None,
             timeout: float | None = None) -> dict:
        """Make one request with synthetic baggage and a fresh traceparent; returns status, latency,
        trace id, transport error and parsed body.
        """
        trace_id = secrets.token_hex(16)
        headers = {"Content-Type": "application/json", "baggage": "synthetic_request=true",
                   "traceparent": f"00-{trace_id}-{secrets.token_hex(8)}-01"}
        data = json.dumps(body).encode() if body is not None else None
        result = {"path": path.split("?")[0], "method": method or ("POST" if data else "GET"),
                  "trace_id": trace_id, "at": datetime.now(timezone.utc).isoformat(),
                  "http_status": None, "transport_error": None}
        started = time.monotonic()
        try:
            with urlopen(Request(self.app_url + path, data=data, headers=headers, method=method),
                         timeout=timeout or self.timeout) as resp:
                raw, result["http_status"] = resp.read(), resp.status
        except HTTPError as error:
            raw, result["http_status"] = error.read(), error.code
        except (URLError, TimeoutError, ConnectionError, OSError) as error:
            reason = getattr(error, "reason", error)
            raw = b""
            result["transport_error"] = "timeout" if isinstance(reason, TimeoutError) or \
                "timed out" in str(reason) else type(reason).__name__
        result["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
        try:
            result["body"] = json.loads(raw) if raw else None
        except ValueError:
            result["body"] = None
        return result

    # ------------------------------------------------------------- shop API
    def add_to_cart(self, user: str, product_id: str, quantity: int) -> dict:
        """Add an item to a user's cart."""
        return self.call("/api/cart", {"userId": user, "item": {"productId": product_id, "quantity": quantity}})

    def read_cart(self, user: str) -> dict:
        """Read a user's cart and report how many items it holds."""
        result = self.call("/api/cart?" + urlencode({"sessionId": user, "currencyCode": "USD"}))
        body = result.pop("body")
        result["items"] = len(body.get("items", [])) if result["http_status"] == 200 and body else None
        return result

    def new_cart(self, product_id: str, quantity: int) -> str:
        """Create a fresh synthetic user with one item in the cart."""
        user = "chaos-" + str(uuid4())
        added = self.add_to_cart(user, product_id, quantity)
        if added["http_status"] != 200:
            raise RuntimeError(f"could not prepare synthetic cart: {added['http_status']}")
        return user

    def cart_items(self, user: str) -> int | None:
        """Number of items in a user's cart (None if it could not be read)."""
        return self.read_cart(user)["items"]

    def checkout(self, user: str, currency: str, card: dict | None = None) -> dict:
        """Place an order with the test address and card; keeps only non-sensitive response fields."""
        order = {"userId": user, "email": SYNTHETIC_EMAIL, "userCurrency": currency,
                 "address": ADDRESS, "creditCard": card or FAKE_CARD}
        result = self.call("/api/checkout?currencyCode=USD", order)
        body = result.pop("body") or {}
        # Keep only non-sensitive response fields.
        result["order_id"] = body.get("orderId")
        result["error"] = body.get("error")
        result["error_code"] = body.get("code")
        return result
