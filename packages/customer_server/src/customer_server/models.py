"""Tool argument models. Each one is the single source of its tool's input schema."""

from __future__ import annotations

import re
from typing import Annotated, TypedDict

from pydantic import BaseModel, ConfigDict, Field, ValidationError

# The assessment writes the id as CUST-XXXXX. I read the X as digits; if that is
# wrong, this is the only line to change. [0-9] and not \d, because \d also
# matches non ASCII digits in some regex engines.
CUSTOMER_ID_PATTERN = r"^CUST-[0-9]{5}$"
REASON_MIN = 10

_ARGUMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


CustomerId = Annotated[
    str,
    Field(pattern=CUSTOMER_ID_PATTERN, description="Customer id, for example CUST-12345."),
]

RefundAmount = Annotated[
    float,
    Field(gt=0, allow_inf_nan=False, description="A finite amount greater than zero."),
]

RefundReason = Annotated[
    str,
    Field(min_length=REASON_MIN, description="Why the refund is due: at least 10 characters."),
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
    ("amount", "finite_number"): "amount must be a finite number",
    ("reason", "string_too_short"): f"reason must be at least {REASON_MIN} characters",
}

_RULE_MESSAGES = {
    "missing": "{field} is required",
    "string_type": "{field} must be a string",
    "extra_forbidden": "{field} is not an argument of this tool",
}


def field_errors(error: ValidationError) -> list[FieldError]:
    """List each failing field and rule. Never the rejected value: echoing it back is a leak."""
    errors: list[FieldError] = []
    for item in error.errors(include_input=False, include_url=False, include_context=False):
        name = str(item["loc"][0]) if item["loc"] else "arguments"
        # An unexpected key is caller text too: name it only if it looks like an identifier.
        field = name if _ARGUMENT_NAME.match(name) else "(unnamed argument)"
        rule = item["type"]
        if (field, rule) in _FIELD_MESSAGES:
            message = _FIELD_MESSAGES[field, rule]
        elif rule in _RULE_MESSAGES:
            message = _RULE_MESSAGES[rule].format(field=field)
        else:
            message = f"{field} is not valid ({rule})"
        errors.append({"field": field, "rule": rule, "message": message})
    return errors
