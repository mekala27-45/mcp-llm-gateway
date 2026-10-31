"""Argument cases the server must reject, shared by the self test and the tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

VALID_REFUND: dict[str, Any] = {
    "customer_id": "CUST-10001",
    "amount": 12.5,
    "reason": "Duplicate charge on the October invoice",
}


@dataclass(frozen=True)
class Rejected:
    name: str
    tool: str
    arguments: dict[str, Any]
    field: str
    rule: str


def _refund(**changes: Any) -> dict[str, Any]:
    return {**VALID_REFUND, **changes}


def _refund_without(key: str) -> dict[str, Any]:
    return {k: v for k, v in VALID_REFUND.items() if k != key}


REJECTED: tuple[Rejected, ...] = (
    Rejected("missing customer_id", "get_customer_record", {}, "customer_id", "missing"),
    Rejected(
        "an extra argument",
        "get_customer_record",
        {"customer_id": "CUST-10001", "note": "hi"},
        "note",
        "extra_forbidden",
    ),
    Rejected(
        "customer_id as a number",
        "get_customer_record",
        {"customer_id": 10001},
        "customer_id",
        "string_type",
    ),
    Rejected(
        "customer_id in lower case",
        "get_customer_record",
        {"customer_id": "cust-10001"},
        "customer_id",
        "string_pattern_mismatch",
    ),
    Rejected(
        "customer_id with a trailing space",
        "get_customer_record",
        {"customer_id": "CUST-10001 "},
        "customer_id",
        "string_pattern_mismatch",
    ),
    Rejected(
        "customer_id with six digits",
        "get_customer_record",
        {"customer_id": "CUST-100011"},
        "customer_id",
        "string_pattern_mismatch",
    ),
    Rejected(
        "customer_id with a Cyrillic letter",
        "get_customer_record",
        {"customer_id": "CUS\u0422-10001"},
        "customer_id",
        "string_pattern_mismatch",
    ),
    Rejected("missing amount", "trigger_refund", _refund_without("amount"), "amount", "missing"),
    Rejected(
        "an extra argument on a refund",
        "trigger_refund",
        _refund(currency="USD"),
        "currency",
        "extra_forbidden",
    ),
    Rejected("amount as a boolean", "trigger_refund", _refund(amount=True), "amount", "float_type"),
    Rejected(
        "amount as a numeric string",
        "trigger_refund",
        _refund(amount="12.50"),
        "amount",
        "float_type",
    ),
    Rejected("a zero amount", "trigger_refund", _refund(amount=0), "amount", "greater_than"),
    Rejected("a negative amount", "trigger_refund", _refund(amount=-5), "amount", "greater_than"),
    Rejected(
        "an amount with three decimal places",
        "trigger_refund",
        _refund(amount=12.345),
        "amount",
        "decimal_places",
    ),
    Rejected(
        "an amount over the cap",
        "trigger_refund",
        _refund(amount=5000.01),
        "amount",
        "less_than_equal",
    ),
    Rejected(
        "a reason of nine characters",
        "trigger_refund",
        _refund(reason="Too short"),
        "reason",
        "string_too_short",
    ),
    Rejected(
        "a reason of ten spaces",
        "trigger_refund",
        _refund(reason=" " * 10),
        "reason",
        "too_short_after_trim",
    ),
    Rejected(
        "a reason over 500 characters",
        "trigger_refund",
        _refund(reason="x" * 501),
        "reason",
        "string_too_long",
    ),
    Rejected(
        "a reason with a control character",
        "trigger_refund",
        _refund(reason="Duplicate charge\x00 on the invoice"),
        "reason",
        "control_characters",
    ),
)
