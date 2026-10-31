import json
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from customer_server.models import GetCustomerRecord, TriggerRefund, field_errors
from customer_server.samples import REJECTED, VALID_REFUND, Rejected

MODELS: dict[str, type[BaseModel]] = {
    "get_customer_record": GetCustomerRecord,
    "trigger_refund": TriggerRefund,
}


def rules(error: ValidationError) -> list[tuple[str, str]]:
    return [(e["field"], e["rule"]) for e in field_errors(error)]


@pytest.mark.parametrize("case", REJECTED, ids=lambda case: case.name)
def test_rejected_arguments_name_the_field_and_the_rule(case: Rejected) -> None:
    with pytest.raises(ValidationError) as caught:
        MODELS[case.tool].model_validate(case.arguments)
    assert (case.field, case.rule) in rules(caught.value)
    assert all(error["message"] for error in field_errors(caught.value))


@pytest.mark.parametrize("amount", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_amounts_are_rejected(amount: float) -> None:
    with pytest.raises(ValidationError) as caught:
        TriggerRefund.model_validate({**VALID_REFUND, "amount": amount})
    assert ("amount", "finite_number") in rules(caught.value)


@pytest.mark.parametrize("amount", [0.01, 1, 19.99, 4999.99, 5000])
def test_amounts_at_the_edges_are_accepted(amount: float) -> None:
    TriggerRefund.model_validate({**VALID_REFUND, "amount": amount})


def test_errors_never_echo_the_rejected_value() -> None:
    bad_value = "CUST-4111111111111111"
    with pytest.raises(ValidationError) as caught:
        GetCustomerRecord.model_validate({"customer_id": bad_value})
    assert bad_value not in json.dumps(field_errors(caught.value))


def test_an_unexpected_argument_is_named_only_if_it_looks_like_an_identifier() -> None:
    arguments: dict[str, Any] = {"customer_id": "CUST-10001", "x\u202e<script>": 1, "note": 2}
    with pytest.raises(ValidationError) as caught:
        GetCustomerRecord.model_validate(arguments)
    fields = {field for field, _ in rules(caught.value)}
    assert fields == {"(unnamed argument)", "note"}


def test_reason_is_stored_trimmed() -> None:
    refund = TriggerRefund.model_validate({**VALID_REFUND, "reason": "  Duplicate charge  "})
    assert refund.reason == "Duplicate charge"


def test_schemas_come_from_the_models_and_forbid_extra_properties() -> None:
    record_schema = GetCustomerRecord.model_json_schema()
    refund_schema = TriggerRefund.model_json_schema()
    assert record_schema["additionalProperties"] is False
    assert refund_schema["additionalProperties"] is False
    assert record_schema["properties"]["customer_id"]["pattern"] == "^CUST-[0-9]{5}$"
    assert refund_schema["properties"]["amount"]["type"] == "number"
    assert refund_schema["properties"]["amount"]["exclusiveMinimum"] == 0
    assert refund_schema["required"] == ["customer_id", "amount", "reason"]
