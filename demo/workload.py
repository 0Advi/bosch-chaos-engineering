"""Synthetic customers on every user-facing path of the shop.

Paths: home, product list, product page, recommendations, ads, image,
international shipping quote, and a buyer flow (add to cart -> view cart ->
checkout). Every request is recorded with its endpoint, phase, status,
latency and W3C trace id.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Callable
from urllib.parse import urlencode
from uuid import uuid4

from controller.observer.endpoints import app_url
from controller.observer.http import Shop

PRODUCTS = ["66VCHSJNUP", "OLJCESPC7Z", "1YMWWN1N4O", "L9ECAV7KIM", "2ZYFJ3GM2N", "OLJCESPC7Z"]
PICTURES = ["NationalParkFoundationExplorascope.jpg", "SolarSystemColorImager.jpg", "RedFlashlight.jpg"]
INTL_ADDRESS = {"streetAddress": "Robert-Bosch-Platz 1", "city": "Gerlingen", "state": "BW",
                "country": "Germany", "zipCode": "70839"}
TIMEOUT_S = 8


class Workload:
    """One independent probe loop per user path, so a hung dependency on one path
    cannot starve measurement of the others; plus a buyer loop for cart+checkout."""

    def __init__(self, emit: Callable[[dict], None], pause_s: float = 0.6):
        self.shop = Shop(app_url(), timeout=TIMEOUT_S)
        self.emit, self.pause_s = emit, pause_s
        self.phase = "baseline"
        self.started = time.monotonic()
        self.stop = threading.Event()
        self.records: list[dict] = []
        self.lock = threading.Lock()
        self.threads: list[threading.Thread] = []
        self.journeys = 0

    def t(self) -> float:
        return round(time.monotonic() - self.started, 2)

    def start(self):
        loops = [(name, call) for name, call in self._browse_calls()] + [("buyer", None)]
        for name, call in loops:
            target = self._buyer if call is None else self._probe
            args = () if call is None else (name, call)
            thread = threading.Thread(target=target, args=args, daemon=True)
            thread.start()
            self.threads.append(thread)
            time.sleep(0.05)

    def halt(self):
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=TIMEOUT_S * 2)

    # ---------------------------------------------------------------- record
    def _record(self, endpoint: str, result: dict, started: tuple[float, str], ok: bool | None = None, **extra):
        # Phase is the one in force when the request *started*.
        status = result.get("http_status")
        row = {"t": self.t(), "t0": round(started[0], 2), "phase": started[1], "endpoint": endpoint,
               "ok": (status == 200) if ok is None else ok, "status": status,
               "timeout": result.get("transport_error") == "timeout",
               "error": result.get("transport_error"), "latency_ms": result.get("latency_ms"),
               "trace_id": result.get("trace_id"), **extra}
        with self.lock:
            self.records.append(row)
        self.emit(row)
        return row

    # ----------------------------------------------------------------- loops
    def _browse_calls(self):
        shop = self.shop

        def product(i):
            return shop.call(f"/api/products/{PRODUCTS[i % len(PRODUCTS)]}?currencyCode=EUR")

        def recommendations(i):
            return shop.call("/api/recommendations?" + urlencode(
                {"productIds": PRODUCTS[i % len(PRODUCTS)], "sessionId": f"chaos-{i}", "currencyCode": "USD"}))

        def shipping(i):
            return shop.call("/api/shipping?" + urlencode({
                "itemList": json.dumps([{"productId": PRODUCTS[i % len(PRODUCTS)], "quantity": 1}]),
                "currencyCode": "EUR", "address": json.dumps(INTL_ADDRESS)}))

        return [("home", lambda i: shop.call("/")),
                ("catalog", lambda i: shop.call("/api/products?currencyCode=EUR")),
                ("product", product), ("recommendations", recommendations),
                ("ads", lambda i: shop.call("/api/data?contextKeys=telescopes")),
                ("image", lambda i: shop.call(f"/images/products/{PICTURES[i % len(PICTURES)]}")),
                ("shipping", shipping)]

    def _probe(self, endpoint: str, call):
        i = 0
        while not self.stop.is_set():
            started = (self.t(), self.phase)
            try:
                result = call(i)
            except Exception as error:
                result = {"http_status": None, "transport_error": type(error).__name__, "latency_ms": None}
            result.pop("body", None)
            self._record(endpoint, result, started)
            i += 1
            self.stop.wait(self.pause_s)

    def _buyer(self):
        shop = self.shop
        while not self.stop.is_set():
            session = "chaos-" + uuid4().hex[:12]
            started = (self.t(), self.phase)
            added = shop.add_to_cart(session, "66VCHSJNUP", 1)
            added.pop("body", None)
            self._record("cart_add", added, started)
            started = (self.t(), self.phase)
            cart = shop.read_cart(session)
            self._record("cart_get", cart, started, ok=cart["http_status"] == 200 and bool(cart.get("items")))
            if added["http_status"] == 200 and not self.stop.is_set():
                started = (self.t(), self.phase)
                order = shop.checkout(session, "USD")
                self._record("checkout", order, started,
                             ok=order["http_status"] == 200 and bool(order.get("order_id")),
                             error_code=order.get("error_code"))
            with self.lock:
                self.journeys += 1
            self.stop.wait(self.pause_s)

    # ------------------------------------------------------------- snapshots
    def window(self, seconds: float) -> list[dict]:
        cutoff = self.t() - seconds
        with self.lock:
            return [r for r in self.records if r["t"] >= cutoff]
