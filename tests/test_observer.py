"""P1-01 fixtures and P1-02 observer, against an in-process fake shop."""
import json

from pydantic import ValidationError
import pytest

from controller.observer.fixtures import CONTROLS, SECRETS, protected_probes, run_attempt
from controller.observer.http import Shop
from controller.observer.observer import observe_checkout
from controller.observer.sanitize import REDACTED, assert_clean, sanitize
from controller.observer.telemetry import Telemetry
from controller.schemas import Observation
from tests.fake_shop import FakeShop


@pytest.fixture
def shop():
    """Fixture: a running fake shop."""
    with FakeShop() as fake:
        yield fake


def clients(fake, logs=None):
    """A shop client and telemetry client for the fake shop."""
    return Shop(fake.url, 5), Telemetry(fake.url + "/jaeger/ui/api", fake.url, timeout=2, log_search=logs)


def test_valid_checkout_places_order_and_clears_cart(shop):
    """A valid checkout places an order and empties the cart."""
    client, _ = clients(shop)
    record = run_attempt(client, "valid", "baseline")
    assert record["matched"], record["checks"]
    assert record["http_status"] == 200 and record["order_id"]
    assert record["cart_items_after"] == 0


@pytest.mark.parametrize("control", CONTROLS)
def test_invalid_card_controls_declined_and_cart_retained(shop, control):
    """Invalid-card controls are declined and the cart is kept."""
    client, _ = clients(shop)
    record = run_attempt(client, control, "control")
    assert record["matched"], record["checks"]
    assert (record["http_status"], record["error_code"]) == (422, "PAYMENT_FAILED")
    assert record["cart_items_after"] == 1


def test_each_attempt_uses_a_fresh_cart(shop):
    """Every attempt uses a new synthetic user and cart."""
    client, _ = clients(shop)
    users = {run_attempt(client, "valid", "baseline")["synthetic_user"] for _ in range(3)}
    assert len(users) == 3


def test_records_never_contain_card_cvv_address_or_email(shop):
    """Attempt records and observations never contain card number, CVV, address or email."""
    client, telemetry = clients(shop)
    records = [run_attempt(client, name, "control") for name in ("valid", *CONTROLS)]
    observations = [observe_checkout(r, telemetry, "run-test", 2, check_logs=False)[0] for r in records]
    assert_clean([records, [o.model_dump(mode="json") for o in observations]], SECRETS)
    text = json.dumps(records)
    assert "creditCard" not in text
    # Compare field values, not raw text: random hex trace ids can contain the digits "672".
    step_values = [v for r in records for step in r["steps"] for v in step.values()]
    assert not any(v in (672, "672") or (isinstance(v, str) and "cvv" in v.lower()) for v in step_values)


def test_complete_observation_links_trace_through_payment(shop):
    """A complete observation links the request's trace through checkout to payment, with logs and
    metrics.
    """
    client, telemetry = clients(shop, logs=lambda tid: {"hits": 2, "excerpts": []})
    obs, detail = observe_checkout(run_attempt(client, "valid", "baseline"), telemetry, "run-test", 2)
    assert obs.complete and obs.fresh and obs.missing == []
    assert {"frontend", "checkout", "payment"} <= set(obs.services)
    assert obs.span_ids == ["c3"] and len(obs.transaction_ids) == 1
    assert obs.expected_outcome_met is True


def test_declined_card_trace_shows_payment_error(shop):
    """A declined card's trace shows the payment error span."""
    client, telemetry = clients(shop, logs=lambda tid: {"hits": 1, "excerpts": []})
    obs, detail = observe_checkout(run_attempt(client, "expired", "control"), telemetry, "run-test", 2)
    assert obs.outcome == "http_error" and obs.http_status == 422 and obs.cart_retained
    assert obs.rpc_code == "2" and obs.transaction_ids == []


def test_missing_trace_is_reported_not_invented(shop):
    """A missing trace is reported as missing rather than invented."""
    shop.drop_traces = True
    client, telemetry = clients(shop, logs=lambda tid: None)
    obs, _ = observe_checkout(run_attempt(client, "valid", "baseline"), telemetry, "run-test", 0.5)
    assert not obs.complete and not obs.fresh
    assert "trace" in obs.missing and "logs_unavailable" in obs.missing
    assert obs.services == [] and obs.transaction_ids == []


def test_unreachable_shop_is_counted_as_a_failed_attempt():
    """An unreachable shop counts as a failed attempt with a transport error."""
    client = Shop("http://127.0.0.1:9", 1)      # nothing listens on the discard port
    record = run_attempt(client, "valid", "baseline")
    assert record["matched"] is False and record["failed_step"] == "add_to_cart"
    obs, _ = observe_checkout(record, None, "run-test")
    assert obs.outcome == "not_observed" and not obs.complete


def test_protected_probes_cover_home_catalog_and_cart(shop):
    """Protected probes cover home, catalog and cart."""
    client, _ = clients(shop)
    results = protected_probes(client)
    assert [r["probe"] for r in results] == ["GET /", "GET /api/products", "POST /api/cart", "GET /api/cart"]
    assert all(r["ok"] for r in results)


def test_sanitize_redacts_cards_emails_and_sensitive_keys():
    """Sanitizing redacts card numbers, emails, CVVs and sensitive keys but keeps trace ids and
    numbers.
    """
    raw = {"body": "card 4432-8015-6152-0454 cvv: 672 for a@b.com", "creditCard": {"x": 1},
           "trace": "0af7651916cd43dd8448eb211c80319c", "latency_ms": 2809.4}
    clean = sanitize(raw)
    assert "4432" not in clean["body"] and "a@b.com" not in clean["body"] and "672" not in clean["body"]
    assert clean["creditCard"] == REDACTED
    assert clean["trace"] == raw["trace"] and clean["latency_ms"] == 2809.4
    with pytest.raises(ValueError):
        assert_clean(raw, SECRETS)


def test_observation_cannot_claim_completeness_with_missing_signals():
    """An observation with missing signals cannot be marked complete."""
    with pytest.raises(ValidationError, match="cannot be complete"):
        Observation(observation_id="obs-1", run_id="run-1", at="2026-10-10T00:00:00Z",
                    probe="POST /api/checkout", phase="baseline", outcome="success",
                    fresh=True, complete=True, missing=["trace"])
