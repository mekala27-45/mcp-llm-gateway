"""Tool argument models. Each one is the single source of its tool's input schema."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Annotated, TypedDict

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError
from pydantic_core import PydanticCustomError

# The assessment writes the id as CUST-XXXXX. I read the X as digits; if that is
# wrong, this is the only line to change. [0-9] and not \d, because \d also
# matches non ASCII digits in some regex engines.
CUSTOMER_ID_PATTERN = r"^CUST-[0-9]{5}$"
REFUND_CAP = Decimal("5000.00")
REASON_MIN = 10
REASON_MAX = 500

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_ARGUMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def _at_most_two_decimal_places(amount: float) -> float:
    # repr is the shortest decimal that round trips, so 19.99 is read as 19.99
    # and not as the binary value 19.989999999999998.
    exponent = Decimal(repr(amount)).as_tuple().exponent
    if isinstance(exponent, int) and exponent < -2:
        raise PydanticCustomError("decimal_places", "amount must have at most two decimal places")
    return amount


def _clean_reason(reason: str) -> str:
    if _CONTROL_CHARS.search(reason):
        raise PydanticCustomError(
            "control_characters", "reason must not contain control characters"
        )
    trimmed = reason.strip()
    if len(trimmed) < REASON_MIN:
        raise PydanticCustomError(
            "too_short_after_trim",
            f"reason must be at least {REASON_MIN} characters after trimming",
        )
    return trimmed


CustomerId = Annotated[
    str,
    Field(pattern=CUSTOMER_ID_PATTERN, description="Customer id, for example CUST-12345."),
]

RefundAmount = Annotated[
    float,
    Field(
        gt=0,
        le=float(REFUND_CAP),
        allow_inf_nan=False,
        description=(
            f"Amount to refund in USD: more than 0, at most {REFUND_CAP}, "
            "with at most two decimal places."
        ),
    ),
    AfterValidator(_at_most_two_decimal_places),
]

RefundReason = Annotated[
    str,
    Field(
        min_length=REASON_MIN,
        max_length=REASON_MAX,
        description=(
            f"Why the refund is due: at least {REASON_MIN} characters after trimming, "
            f"at most {REASON_MAX}, no control characters."
        ),
        json_schema_extra={"pattern": r"^[^\u0000-\u001F\u007F-\u009F]*$"},
    ),
    AfterValidator(_clean_reason),
]


class GetCustomerRecord(BaseModel):
    """Arguments of get_customer_record."""

    model_config = ConfigDict(strict=True, extra="forbid", title="get_customer_record")

    customer_id: CustomerId


class TriggerRefund(BaseModel):
    """Arguments of trigger_refund."""

    model_config = ConfigDict(strict=True, extra="forbid", title="trigger_refund")

    customer_id: CustomerId
    amount: RefundAmount
    reason: RefundReason


class FieldError(TypedDict):
    field: str
    rule: str
    message: str


_FIELD_MESSAGES: dict[tuple[str, str], str] = {
    ("customer_id", "string_pattern_mismatch"): "customer_id must look like CUST-12345",
    ("amount", "float_type"): "amount must be a JSON number",
    ("amount", "greater_than"): "amount must be greater than 0",
    ("amount", "less_than_equal"): f"amount must be at most {REFUND_CAP}",
    ("amount", "finite_number"): "amount must be a finite number",
    ("reason", "string_too_short"): f"reason must be at least {REASON_MIN} characters",
    ("reason", "string_too_long"): f"reason must be at most {REASON_MAX} characters",
}

_RULE_MESSAGES = {
    "missing": "{field} is required",
    "string_type": "{field} must be a string",
    "extra_forbidden": "{field} is not an argument of this tool",
}

_CUSTOM_RULES = {"decimal_places", "control_characters", "too_short_after_trim"}


def field_errors(error: ValidationError) -> list[FieldError]:
    """List each failing field and rule. Never the rejected value: echoing it back is a leak."""
    errors: list[FieldError] = []
    for item in error.errors(include_input=False, include_url=False, include_context=False):
        name = str(item["loc"][0]) if item["loc"] else "arguments"
        # An unexpected key is caller text too: name it only if it looks like an identifier.
        field = name if _ARGUMENT_NAME.match(name) else "(unnamed argument)"
        rule = item["type"]
        if rule in _CUSTOM_RULES:
            message = item["msg"]
        elif (field, rule) in _FIELD_MESSAGES:
            message = _FIELD_MESSAGES[field, rule]
        elif rule in _RULE_MESSAGES:
            message = _RULE_MESSAGES[rule].format(field=field)
        else:
            message = f"{field} is not valid ({rule})"
        errors.append({"field": field, "rule": rule, "message": message})
    return errors
