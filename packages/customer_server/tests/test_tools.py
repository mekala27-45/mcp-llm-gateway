import logging

import pytest
from mcp import Client, MCPError
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS

from customer_server.ledger import Ledger
from customer_server.samples import VALID_REFUND
from customer_server.server import build_server


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


async def test_lists_both_tools_with_schemas_from_the_models() -> None:
    async with Client(build_server()) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert set(tools) == {"get_customer_record", "trigger_refund"}
    assert tools["trigger_refund"].input_schema["additionalProperties"] is False


async def test_reads_a_customer_record() -> None:
    async with Client(build_server()) as client:
        result = await client.call_tool("get_customer_record", {"customer_id": "CUST-10001"})
    assert not result.is_error
    assert result.structured_content["name"] == "Ada Lindqvist"
    assert result.structured_content["refundable_balance"] == "420.00"


async def test_a_refund_reduces_the_refundable_balance() -> None:
    async with Client(build_server()) as client:
        result = await client.call_tool("trigger_refund", VALID_REFUND)
    assert not result.is_error
    assert result.structured_content["amount"] == "12.50"
    assert result.structured_content["refundable_balance"] == "407.50"
    assert result.structured_content["replayed"] is False


async def test_a_repeated_refund_within_a_minute_returns_the_original_id() -> None:
    clock = FakeClock()
    async with Client(build_server(Ledger(clock))) as client:
        first = await client.call_tool("trigger_refund", VALID_REFUND)
        clock.now += 59.9
        again = await client.call_tool("trigger_refund", VALID_REFUND)
    assert again.structured_content["refund_id"] == first.structured_content["refund_id"]
    assert again.structured_content["replayed"] is True
    assert again.structured_content["refundable_balance"] == "407.50"


async def test_the_same_refund_a_minute_later_is_a_new_refund() -> None:
    clock = FakeClock()
    async with Client(build_server(Ledger(clock))) as client:
        first = await client.call_tool("trigger_refund", VALID_REFUND)
        clock.now += 60.0
        again = await client.call_tool("trigger_refund", VALID_REFUND)
    assert again.structured_content["refund_id"] != first.structured_content["refund_id"]
    assert again.structured_content["refundable_balance"] == "395.00"


async def test_an_unknown_customer_is_a_tool_error_not_a_protocol_error() -> None:
    async with Client(build_server()) as client:
        result = await client.call_tool("get_customer_record", {"customer_id": "CUST-99999"})
    assert result.is_error


async def test_a_refund_for_an_unknown_customer_is_a_tool_error() -> None:
    async with Client(build_server()) as client:
        result = await client.call_tool(
            "trigger_refund", {**VALID_REFUND, "customer_id": "CUST-99999"}
        )
    assert result.is_error


async def test_a_refund_over_the_balance_is_a_tool_error() -> None:
    async with Client(build_server()) as client:
        result = await client.call_tool(
            "trigger_refund", {**VALID_REFUND, "customer_id": "CUST-10004"}
        )
    assert result.is_error
    assert "refundable balance of 0.00" in result.content[0].model_dump()["text"]


async def test_an_unknown_tool_is_a_protocol_error() -> None:
    async with Client(build_server()) as client:
        with pytest.raises(MCPError) as caught:
            await client.call_tool("delete_customer", {"customer_id": "CUST-10001"})
    assert caught.value.code == INVALID_PARAMS
    assert caught.value.message == "Unknown tool"


async def test_invalid_arguments_are_invalid_params_with_the_failing_fields() -> None:
    async with Client(build_server()) as client:
        with pytest.raises(MCPError) as caught:
            await client.call_tool(
                "trigger_refund", {**VALID_REFUND, "amount": 0, "reason": "short"}
            )
    assert caught.value.code == INVALID_PARAMS
    failing = {(e["field"], e["rule"]) for e in caught.value.data["errors"]}
    assert failing == {("amount", "greater_than"), ("reason", "string_too_short")}


async def test_tool_mode_reports_invalid_arguments_as_a_tool_error() -> None:
    async with Client(build_server(mode="tool")) as client:
        result = await client.call_tool("trigger_refund", {**VALID_REFUND, "amount": 0})
    assert result.is_error
    assert result.structured_content["errors"][0]["field"] == "amount"
    assert "amount must be greater than 0" in result.content[0].model_dump()["text"]


async def test_an_unexpected_failure_is_a_generic_internal_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger="customer_server")
    async with Client(build_server(test_tools=True)) as client:
        with pytest.raises(MCPError) as caught:
            await client.call_tool("debug_crash", {})
    assert caught.value.code == INTERNAL_ERROR
    assert caught.value.message == "Internal error"
    assert caught.value.data is None
    assert any(record.exc_info for record in caplog.records)


@pytest.mark.parametrize(
    ("amount", "expected_amount", "expected_balance"),
    [
        (25.001, "25.001", "394.999"),
        (1e-30, "0." + "0" * 29 + "1", "419." + "9" * 30),
        (5e-324, "0." + "0" * 323 + "5", "419." + "9" * 323 + "5"),
    ],
)
async def test_positive_floats_are_refunded_without_rounding(
    amount: float, expected_amount: str, expected_balance: str
) -> None:
    async with Client(build_server()) as client:
        refund = await client.call_tool("trigger_refund", {**VALID_REFUND, "amount": amount})
        record = await client.call_tool("get_customer_record", {"customer_id": "CUST-10001"})
    assert not refund.is_error
    assert refund.structured_content["amount"] == expected_amount
    assert refund.structured_content["refundable_balance"] == expected_balance
    assert record.structured_content["refundable_balance"] == expected_balance


async def test_amount_above_old_cap_reaches_the_business_rule() -> None:
    async with Client(build_server()) as client:
        result = await client.call_tool("trigger_refund", {**VALID_REFUND, "amount": 5000.01})
    assert result.is_error
    assert "refundable balance" in result.content[0].model_dump()["text"]


async def test_distinct_subcent_refunds_do_not_share_a_replay_key() -> None:
    async with Client(build_server()) as client:
        first = await client.call_tool("trigger_refund", {**VALID_REFUND, "amount": 25.001})
        second = await client.call_tool("trigger_refund", {**VALID_REFUND, "amount": 25.002})
        replay = await client.call_tool("trigger_refund", {**VALID_REFUND, "amount": 25.001})
    assert first.structured_content["refund_id"] != second.structured_content["refund_id"]
    assert second.structured_content["refundable_balance"] == "369.997"
    assert replay.structured_content["refund_id"] == first.structured_content["refund_id"]
    assert replay.structured_content["refundable_balance"] == "369.997"
