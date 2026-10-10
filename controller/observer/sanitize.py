"""Keep raw card, CVV, address and email data out of logs and artifacts (P1-01)."""
from __future__ import annotations

import json
import re

REDACTED = "[redacted]"
SENSITIVE_KEYS = frozenset({
    "creditcard", "creditcardnumber", "creditcardcvv", "creditcardexpirationyear",
    "creditcardexpirationmonth", "email", "address", "streetaddress", "zipcode",
    "user.email", "demo.payment.card_number", "demo.payment.card_cvv",
    "card_number", "card_cvv", "cvv",
})
# 13-19 digits optionally separated by spaces/hyphens, i.e. a card-number shape.
CARD_RE = re.compile(r"(?<![\w-])(?:\d[ -]?){12,18}\d(?![\w-])")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
CVV_RE = re.compile(r"(?i)(cvv\W{0,3})\d{3,4}")


def redact_text(text: str) -> str:
    text = CARD_RE.sub(REDACTED, text)
    text = EMAIL_RE.sub(REDACTED, text)
    return CVV_RE.sub(lambda m: m.group(1) + REDACTED, text)


def sanitize(value):
    """Recursively drop sensitive keys and redact card/email/CVV shapes in text.

    Apply this to external text (error bodies, log records, span attributes),
    not to numeric measurements.
    """
    if isinstance(value, dict):
        return {k: REDACTED if k.lower() in SENSITIVE_KEYS else sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def assert_clean(document, secrets: list[str]) -> None:
    """Raise if any known fixture secret appears anywhere in a document."""
    text = json.dumps(document, default=str)
    compact = re.sub(r"[ -]", "", text)
    for secret in secrets:
        if not secret:
            continue
        if secret in text or re.sub(r"[ -]", "", secret) in compact:
            raise ValueError("sanitization failed: fixture secret found in evidence")
