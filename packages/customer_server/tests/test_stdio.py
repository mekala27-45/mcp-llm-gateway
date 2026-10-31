import os
import subprocess
import sys

import pytest
from mcp import MCPError
from mcp.types import INVALID_PARAMS
from stdio_session import StdioSession

from customer_server.samples import REJECTED, VALID_REFUND, Rejected


def test_lists_both_tools_over_stdio(protocol_session: StdioSession) -> None:
    tools = {tool.name: tool for tool in protocol_session.list_tools().tools}
    assert set(tools) == {"get_customer_record", "trigger_refund"}
    schema = tools["get_customer_record"].input_schema
    assert schema["additionalProperties"] is False
    assert schema["properties"]["customer_id"]["pattern"] == "^CUST-[0-9]{5}$"


def test_calls_both_tools_over_stdio(protocol_session: StdioSession) -> None:
    record = protocol_session.call("get_customer_record", {"customer_id": "CUST-10002"})
    refund = protocol_session.call("trigger_refund", {**VALID_REFUND, "customer_id": "CUST-10002"})
    assert not record.is_error
    assert not refund.is_error
    assert refund.structured_content["refund_id"].startswith("RF-")


@pytest.mark.parametrize("case", REJECTED, ids=lambda case: case.name)
def test_invalid_arguments_get_invalid_params_with_the_field(
    protocol_session: StdioSession, case: Rejected
) -> None:
    with pytest.raises(MCPError) as caught:
        protocol_session.call(case.tool, case.arguments)
    assert caught.value.code == INVALID_PARAMS
    failing = [(e["field"], e["rule"]) for e in caught.value.data["errors"]]
    assert (case.field, case.rule) in failing


@pytest.mark.parametrize("case", REJECTED, ids=lambda case: case.name)
def test_tool_mode_returns_invalid_arguments_as_a_tool_error(
    tool_session: StdioSession, case: Rejected
) -> None:
    result = tool_session.call(case.tool, case.arguments)
    assert result.is_error
    failing = [(e["field"], e["rule"]) for e in result.structured_content["errors"]]
    assert (case.field, case.rule) in failing


def test_a_business_failure_is_a_tool_error_in_both_modes(
    protocol_session: StdioSession, tool_session: StdioSession
) -> None:
    for session in (protocol_session, tool_session):
        result = session.call("trigger_refund", {**VALID_REFUND, "customer_id": "CUST-10004"})
        assert result.is_error


def test_self_test_passes_and_writes_nothing_to_stdout() -> None:
    finished = subprocess.run(
        [sys.executable, "-m", "customer_server", "--self-test"],
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert finished.returncode == 0, finished.stderr.decode()
    assert finished.stdout == b""
    assert b"checks passed" in finished.stderr


def test_an_unknown_validation_mode_stops_the_server_before_it_serves() -> None:
    finished = subprocess.run(
        [sys.executable, "-m", "customer_server"],
        input=b"",
        capture_output=True,
        timeout=60,
        env={**os.environ, "VALIDATION_ERRORS": "loud"},
        check=False,
    )
    assert finished.returncode == 2
    assert finished.stdout == b""
    assert b"VALIDATION_ERRORS must be protocol or tool" in finished.stderr
