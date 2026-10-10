"""Synthetic checkout fixtures (P1-01).

Each attempt uses a fresh synthetic user/cart, the same product, quantity and
currency, and the upstream demo's fake card data. Invalid-card controls must be
declined by payment validation with the cart retained. Records keep only IDs,
status codes, latencies and cart counts; no card, CVV, address or email.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

from .http import ADDRESS, FAKE_CARD, SYNTHETIC_EMAIL, Shop

WORKLOAD_VERSION = "checkout-fixtures-1"
PRODUCT_ID = "66VCHSJNUP"   # fixed catalog item, not the productCatalogFailure target
QUANTITY = 1
CURRENCY = "USD"


@dataclass(frozen=True)
class Fixture:
    name: str
    card: dict
    expect_status: int
    expect_error_code: str | None
    expect_cart_retained: bool
    expect_order: bool
    description: str = field(default="")


FIXTURES = {
    "valid": Fixture("valid", FAKE_CARD, 200, None, False, True,
                     "upstream fake VISA; order placed and cart cleared"),
    "invalid_number": Fixture(
        "invalid_number", {**FAKE_CARD, "creditCardNumber": "4432-8015-6152-0455"},
        422, "PAYMENT_FAILED", True, False, "fails card-number validation"),
    "expired": Fixture(
        "expired", {**FAKE_CARD, "creditCardExpirationYear": 2020}, 422, "PAYMENT_FAILED",
        True, False, "valid number, expired 1/2020"),
    "unsupported_type": Fixture(
        "unsupported_type", {"creditCardNumber": "3782-822463-10005", "creditCardCvv": 1234,
                             "creditCardExpirationYear": 2039, "creditCardExpirationMonth": 1},
        422, "PAYMENT_FAILED", True, False, "AMEX test number; payment accepts only VISA/MasterCard"),
}
CONTROLS = ("invalid_number", "expired", "unsupported_type")
# Values that must never appear in saved evidence.
SECRETS = sorted({f.card["creditCardNumber"] for f in FIXTURES.values()}
                 | {SYNTHETIC_EMAIL, ADDRESS["streetAddress"]})


def _step(name: str, result: dict) -> dict:
    return {"name": name, "http_status": result["http_status"], "latency_ms": result["latency_ms"],
            "trace_id": result["trace_id"], "transport_error": result["transport_error"]}


def run_attempt(shop: Shop, fixture_name: str, phase: str) -> dict:
    """One complete attempt: fresh cart -> checkout -> read cart. Never raises on app errors."""
    fixture = FIXTURES[fixture_name]
    user = "chaos-" + str(uuid4())
    record = {
        "attempt_id": "att-" + uuid4().hex[:12], "phase": phase, "fixture": fixture.name,
        "workload_version": WORKLOAD_VERSION, "product_id": PRODUCT_ID, "currency": CURRENCY,
        "synthetic_user": user, "expected": {
            "http_status": fixture.expect_status, "error_code": fixture.expect_error_code,
            "cart_retained": fixture.expect_cart_retained, "order": fixture.expect_order},
    }
    added = shop.add_to_cart(user, PRODUCT_ID, QUANTITY)
    record["steps"] = [_step("add_to_cart", added)]
    record["at"] = added["at"]
    if added["http_status"] != 200:
        record.update(failed_step="add_to_cart", http_status=added["http_status"],
                      transport_error=added["transport_error"], latency_ms=None, trace_id=None,
                      order_id=None, error_code=None, cart_items_after=None, matched=False)
        return record

    checkout = shop.checkout(user, CURRENCY, fixture.card)
    cart = shop.read_cart(user)
    record["steps"] += [_step("checkout", checkout), _step("read_cart", cart)]
    record.update(
        failed_step=None, trace_id=checkout["trace_id"], http_status=checkout["http_status"],
        transport_error=checkout["transport_error"], latency_ms=checkout["latency_ms"],
        order_id=checkout["order_id"], error_code=checkout["error_code"],
        cart_items_after=cart["items"])
    checks = {
        "status": checkout["http_status"] == fixture.expect_status,
        "error_code": checkout["error_code"] == fixture.expect_error_code,
        "order": bool(checkout["order_id"]) == fixture.expect_order,
        "cart": cart["items"] is not None and (cart["items"] > 0) == fixture.expect_cart_retained,
    }
    record["checks"] = checks
    record["matched"] = all(checks.values())
    return record


PROTECTED_PROBES = ("GET /", "GET /api/products", "POST /api/cart", "GET /api/cart")


def protected_probes(shop: Shop, timeout: float | None = None) -> list[dict]:
    """Paths that must stay healthy while checkout/payment is faulted."""
    user = "chaos-probe-" + uuid4().hex[:12]
    results = []
    for probe, call in [
        ("GET /", lambda: shop.call("/", timeout=timeout)),
        ("GET /api/products", lambda: shop.call("/api/products?currencyCode=USD", timeout=timeout)),
        ("POST /api/cart", lambda: shop.add_to_cart(user, PRODUCT_ID, QUANTITY)),
        ("GET /api/cart", lambda: shop.read_cart(user)),
    ]:
        result = call()
        result.pop("body", None)
        results.append({"probe": probe, "ok": result["http_status"] == 200, **result})
    return results
