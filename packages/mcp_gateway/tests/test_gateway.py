import json
import logging
import time
from typing import Any

import httpx
import httpx2
import pytest
from gateway_stack import (
    ENVELOPE,
    SECRET,
    Stack,
    headers_for,
    rpc,
    running_stack,
    tool_call,
)
from mcp import Client, MCPError
from mcp.client.streamable_http import streamable_http_client
from mcp.types import PROTOCOL_VERSION_META_KEY, UNSUPPORTED_PROTOCOL_VERSION

from gateway_common.serving import background_server
from mcp_gateway.app import create_app
from mcp_gateway.auth import mint_token
from mcp_gateway.inspection import UNAUTHORIZED_TOOL_CALL
from mcp_gateway.settings import Settings


def post(stack: Stack, role: str, body: dict[str, Any]) -> httpx.Response:
    return httpx.post(stack.url, json=body, headers=headers_for(stack.token(role), body))


def test_admin_calls_admin_reset_key_and_it_reaches_the_server(stack: Stack) -> None:
    before = len(stack.tool_calls())
    response = post(stack, "admin", tool_call("admin_reset_key", key_id="key-1"))
    assert response.status_code == 200
    assert response.json()["result"]["structuredContent"] == {"key_id": "key-1", "status": "reset"}
    assert stack.tool_calls()[before:] == ["admin_reset_key"]


def test_viewer_calls_get_customer_record_and_it_reaches_the_server(stack: Stack) -> None:
    before = len(stack.tool_calls())
    response = post(stack, "viewer", tool_call("get_customer_record", customer_id="CUST-10001"))
    assert response.status_code == 200
    assert not response.json()["result"]["isError"]
    assert stack.tool_calls()[before:] == ["get_customer_record"]


def test_viewer_cannot_call_admin_tool(stack: Stack) -> None:
    before = len(stack.log)
    response = post(stack, "viewer", tool_call("admin_reset_key", request_id="abc", key_id="key-1"))
    assert response.status_code == 403
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": "abc",
        "error": {
            "code": UNAUTHORIZED_TOOL_CALL,
            "message": "Unauthorized Tool Call",
            "data": {"tool": "admin_reset_key", "role": "viewer"},
        },
    }
    assert len(stack.log) == before


@pytest.mark.parametrize("role", ["admin", "viewer"])
def test_tools_list_is_forwarded_for_both_roles(stack: Stack, role: str) -> None:
    response = post(stack, role, rpc("tools/list"))
    names = {tool["name"] for tool in response.json()["result"]["tools"]}
    assert names == {"get_customer_record", "admin_reset_key", "slow_report"}


def test_server_discover_is_forwarded(stack: Stack) -> None:
    response = post(stack, "viewer", rpc("server/discover"))
    assert "2026-07-28" in response.json()["result"]["supportedVersions"]


def test_a_notification_is_forwarded_and_answered_202(stack: Stack) -> None:
    before = len(stack.log)
    body = {
        "jsonrpc": "2.0",
        "method": "notifications/cancelled",
        "params": {"requestId": 1, "_meta": ENVELOPE},
    }
    response = post(stack, "viewer", body)
    assert response.status_code == 202
    assert response.content == b""
    assert len(stack.log) == before + 1


def test_sse_streams_through_before_the_downstream_finishes(stack: Stack) -> None:
    body = rpc("tools/call", name="slow_report", arguments={"steps": 4})
    body["params"]["_meta"] = {**ENVELOPE, "progressToken": "report-1"}
    started = time.monotonic()
    first_event_at = None
    events = []
    headers = headers_for(stack.token("viewer"), body)
    with httpx.stream("POST", stack.url, json=body, headers=headers) as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        for line in response.iter_lines():
            if line.startswith("data:"):
                first_event_at = first_event_at or time.monotonic() - started
                events.append(json.loads(line[5:]))
    finished_at = time.monotonic() - started

    assert events[0]["method"] == "notifications/progress"
    assert events[-1]["result"]["content"][0]["text"] == "report ready after 4 steps"
    assert first_event_at is not None
    assert first_event_at < finished_at / 2


def test_an_unauthenticated_request_gets_401_with_a_bearer_challenge(stack: Stack) -> None:
    before = len(stack.log)
    response = httpx.post(stack.url, json=rpc("tools/list"), headers=headers_for(None))
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Bearer realm="mcp-gateway"'
    assert len(stack.log) == before


def test_every_decision_writes_one_audit_line_without_the_token(
    stack: Stack, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="mcp_gateway.audit")
    token = stack.token("viewer")
    body = tool_call("admin_reset_key", key_id="key-1")
    httpx.post(stack.url, json=body, headers=headers_for(token, body))

    decisions = [r for r in caplog.records if r.name == "mcp_gateway.audit"]
    assert len(decisions) == 1
    fields = decisions[0].__dict__["fields"]
    assert fields["decision"] == "denied"
    assert fields["role"] == "viewer"
    assert fields["tool"] == "admin_reset_key"
    assert len(fields["token_fp"]) == 8
    assert token not in json.dumps(fields)


async def test_an_sdk_client_works_through_the_gateway(stack: Stack) -> None:
    auth = {"authorization": f"Bearer {stack.token('viewer')}"}
    async with (
        httpx2.AsyncClient(headers=auth) as http,
        Client(streamable_http_client(stack.url, http_client=http)) as client,
    ):
        tools = await client.list_tools()
        record = await client.call_tool("get_customer_record", {"customer_id": "CUST-10001"})
        with pytest.raises(MCPError) as refused:
            await client.call_tool("admin_reset_key", {"key_id": "key-1"})
    assert len(tools.tools) == 3
    assert not record.is_error
    assert refused.value.code == UNAUTHORIZED_TOOL_CALL
    assert refused.value.message == "Unauthorized Tool Call"


def test_a_downstream_that_is_down_gets_a_generic_502() -> None:
    settings = Settings(jwt_secret=SECRET, downstream_url="http://127.0.0.1:9/mcp")
    body = rpc("tools/list")
    headers = headers_for(mint_token(settings, "viewer", "test-viewer", 300), body)
    with background_server(create_app(settings)) as gateway:
        response = httpx.post(f"{gateway}/mcp", json=body, headers=headers)
    assert response.status_code == 502
    assert response.json()["error"] == {"code": -32603, "message": "Downstream server unavailable"}
    assert "127.0.0.1" not in response.text


def test_a_downstream_error_that_is_not_json_rpc_is_replaced() -> None:
    with running_stack(downstream_token="wrong-credential") as stack:
        response = post(stack, "viewer", rpc("tools/list"))
    assert response.status_code == 502
    assert response.json()["error"] == {"code": -32603, "message": "Downstream server error"}
    assert "unauthorized" not in response.text


def test_a_json_rpc_error_from_the_downstream_passes_through(stack: Stack) -> None:
    body = rpc("tools/list")
    body["params"]["_meta"] = {**ENVELOPE, PROTOCOL_VERSION_META_KEY: "2099-01-01"}
    headers = {**headers_for(stack.token("viewer"), body), "mcp-protocol-version": "2099-01-01"}
    response = httpx.post(stack.url, json=body, headers=headers)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == UNSUPPORTED_PROTOCOL_VERSION


def test_listed_tools_only_fails_closed_when_the_listing_cannot_be_read() -> None:
    settings = Settings(
        jwt_secret=SECRET, downstream_url="http://127.0.0.1:9/mcp", listed_tools_only=True
    )
    body = tool_call("get_customer_record", customer_id="CUST-10001")
    headers = headers_for(mint_token(settings, "admin", "test-admin", 300), body)
    with background_server(create_app(settings)) as gateway:
        response = httpx.post(f"{gateway}/mcp", json=body, headers=headers)
    assert response.status_code == 502
    assert response.json()["error"]["message"] == "Tool listing unavailable"
