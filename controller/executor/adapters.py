"""Allowlisted fault adapters.

Every action is built from the catalog's fixed adapter_config plus parameters
already validated against the capability. Nothing here accepts a command,
path, URL or container name from a plan or model.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import secrets
import subprocess
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

from ..schemas import AdapterKind, Capability
from .flag_store import FlagSnapshot, FlagStore

ROOT = Path(__file__).resolve().parents[2]
PROTO_DIR = ROOT / "vendor" / "opentelemetry-demo" / "pb"
# Upstream demo test-only card (same as scripts/verify_startup.py); not a real card.
FAKE_CARD = {"creditCardNumber": "4432-8015-6152-0454", "creditCardCvv": 672,
             "creditCardExpirationYear": 2039, "creditCardExpirationMonth": 1}
ADDRESS = {"streetAddress": "1600 Amphitheatre Parkway", "zipCode": "94043",
           "city": "Mountain View", "state": "CA", "country": "United States"}
MONEY_MODES = {
    "valid_usd": {"currencyCode": "USD", "units": "10", "nanos": 0},
    "negative_amount": {"currencyCode": "USD", "units": "-100", "nanos": 0},
    "nanos_out_of_range": {"currencyCode": "USD", "units": "10", "nanos": 2000000000},
    "sign_mismatch": {"currencyCode": "USD", "units": "10", "nanos": -500000000},
    "unknown_currency": {"currencyCode": "XXX", "units": "10", "nanos": 0},
    "zero_amount": {"currencyCode": "USD", "units": "0", "nanos": 0},
}


# ------------------------------------------------------------------- flags
def flag_variant(cap: Capability, params: dict) -> tuple[str, str]:
    cfg = cap.adapter_config
    return cfg["flag"], params.get("failure_rate", cfg.get("fault_variant"))


def flag_apply(store: FlagStore, cap: Capability, params: dict) -> tuple[FlagSnapshot, FlagSnapshot]:
    flag, variant = flag_variant(cap, params)
    prior = store.snapshot(flag)
    applied = store.set_variant(flag, variant, prior.sha256)
    return prior, applied


# -------------------------------------------------------- checkout fixture
class Shop:
    """Synthetic HTTP client for the loopback frontend-proxy only."""

    def __init__(self, app_url: str, timeout: float):
        if not app_url.startswith(("http://127.0.0.1:", "http://localhost:")):
            raise PermissionError("checkout fixtures only target the loopback sandbox proxy")
        self.app_url, self.timeout = app_url.rstrip("/"), timeout

    def call(self, path: str, body: dict | None = None) -> dict:
        trace_id = secrets.token_hex(16)
        headers = {"Content-Type": "application/json", "baggage": "synthetic_request=true",
                   "traceparent": f"00-{trace_id}-{secrets.token_hex(8)}-01"}
        data = json.dumps(body).encode() if body is not None else None
        started = time.monotonic()
        result = {"path": path.split("?")[0], "trace_id": trace_id}
        try:
            with urlopen(Request(self.app_url + path, data=data, headers=headers),
                         timeout=self.timeout) as resp:
                raw, result["http_status"] = resp.read(), resp.status
        except HTTPError as error:
            raw, result["http_status"] = error.read(), error.code
        except (URLError, TimeoutError) as error:
            raw, result["http_status"], result["transport_error"] = b"", None, type(error).__name__
        result["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
        try:
            result["body"] = json.loads(raw) if raw else None
        except ValueError:
            result["body"] = None
        return result

    def new_cart(self, product_id: str, quantity: int) -> str:
        user = "chaos-" + str(uuid4())
        added = self.call("/api/cart", {"userId": user, "item": {"productId": product_id, "quantity": quantity}})
        if added["http_status"] != 200:
            raise RuntimeError(f"could not prepare synthetic cart: {added['http_status']}")
        return user

    def cart_items(self, user: str) -> int | None:
        cart = self.call("/api/cart?" + urlencode({"sessionId": user, "currencyCode": "USD"}))
        return len(cart["body"].get("items", [])) if cart["http_status"] == 200 and cart["body"] else None

    def checkout(self, user: str, currency: str) -> dict:
        order = {"userId": user, "email": "chaos-synthetic@example.com", "userCurrency": currency,
                 "address": ADDRESS, "creditCard": FAKE_CARD}
        result = self.call("/api/checkout?currencyCode=USD", order)
        body = result.pop("body") or {}
        # Keep only non-sensitive fields from the response.
        result["order_id"] = body.get("orderId")
        result["error"] = body.get("error")
        result["error_code"] = body.get("code")
        return result


def currency_mismatch(shop: Shop, cap: Capability, params: dict) -> list[dict]:
    cfg, results = cap.adapter_config, []
    for _ in range(params["attempts"]):
        user = shop.new_cart(cfg["product_id"], cfg["quantity"])
        outcome = shop.checkout(user, params["currency_code"])
        outcome["cart_items_after"] = shop.cart_items(user)
        results.append(outcome)
    return results


def duplicate_submit(shop: Shop, cap: Capability, params: dict) -> list[dict]:
    cfg, n = cap.adapter_config, params["concurrency"]
    user = shop.new_cart(cfg["product_id"], cfg["quantity"])
    gate = threading.Barrier(n)

    def fire(_):
        gate.wait(timeout=5)
        return shop.checkout(user, cfg["currency_code"])

    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(fire, range(n)))
    accepted = [r["order_id"] for r in results if r["http_status"] == 200 and r["order_id"]]
    return [{"logical_cart": user, "requests": n, "accepted_orders": accepted,
             "duplicate_accepted": len(accepted) > 1,
             "cart_items_after": shop.cart_items(user), "attempts": results}]


def valid_checkout(shop: Shop, product_id: str = "66VCHSJNUP") -> dict:
    user = shop.new_cart(product_id, 1)
    result = shop.checkout(user, "USD")
    result["ok"] = result["http_status"] == 200 and bool(result["order_id"])
    return result


# ------------------------------------------------------ payment gRPC fixture
def grpc_argv(cap: Capability, params: dict, network: str, run_id: str, timeout: float) -> list[str]:
    cfg = cap.adapter_config
    payload = json.dumps({"amount": MONEY_MODES[params["mode"]], "creditCard": FAKE_CARD})
    return ["docker", "run", "--rm", "--network", network, "--label", f"bosch.chaos.run={run_id}",
            "-v", f"{PROTO_DIR}:/protos:ro", cfg["grpcurl_image"],
            "-plaintext", "-max-time", str(int(timeout)), "-format-error",
            "-import-path", "/protos", "-proto", "demo.proto",
            "-H", "baggage: synthetic_request=true", "-d", payload,
            cfg["endpoint"], cfg["method"]]


def payment_charge(cap: Capability, params: dict, network: str, run_id: str, timeout: float) -> list[dict]:
    argv, results = grpc_argv(cap, params, network, run_id, timeout), []
    for attempt in range(params.get("repeat", 1)):
        started = time.monotonic()
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout + 30)
        try:
            body = json.loads(proc.stdout or proc.stderr or "{}")
        except ValueError:
            body = {"raw": (proc.stdout + proc.stderr)[-500:]}
        results.append({"attempt": attempt + 1, "mode": params["mode"], "exit_code": proc.returncode,
                        "transaction_id": body.get("transactionId"),
                        "rpc_code": body.get("code"), "rpc_message": body.get("message"),
                        "latency_ms": round((time.monotonic() - started) * 1000, 1)})
    ids = [r["transaction_id"] for r in results if r["transaction_id"]]
    if len(results) > 1:
        results.append({"summary": True, "identical_requests": len(results),
                        "distinct_transaction_ids": len(set(ids))})
    return results


# --------------------------------------------------------------- describe
def describe(cap: Capability, params: dict, network: str = "bosch-chaos-sandbox",
             app_url: str = "http://127.0.0.1:8080") -> list[str]:
    """Human-readable dry-run of exactly what a live run would do."""
    if cap.adapter == AdapterKind.FLAG:
        flag, variant = flag_variant(cap, params)
        return [f"snapshot deploy/flagd/demo.flagd.json and {flag} prior variant",
                f"atomically set {flag}.defaultVariant={variant!r} (hash-guarded)",
                "hold for duration_seconds while probing checkout",
                f"restore {flag} prior variant (hash-guarded) and verify valid checkouts"]
    if cap.id == "checkout-currency-mismatch":
        return [f"{params['attempts']}x: new synthetic cart, POST {app_url}/api/checkout "
                f"userCurrency={params['currency_code']!r}, then read cart"]
    if cap.id == "checkout-duplicate-submit":
        return [f"one synthetic cart; {params['concurrency']} identical POST {app_url}/api/checkout "
                "released together by a barrier; record accepted order IDs and cart"]
    if cap.adapter == AdapterKind.PAYMENT_GRPC:
        return [" ".join(grpc_argv(cap, params, network, "<run-id>", 10))
                + f"  (x{params.get('repeat', 1)})"]
    return [f"{cap.adapter.value}: no executable adapter"]
