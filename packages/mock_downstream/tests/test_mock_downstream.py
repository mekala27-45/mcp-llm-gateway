import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from gateway_common.serving import background_server
from mock_downstream.app import CallLog, build_app

TOKEN = "downstream-test-credential"


async def test_serves_the_tools_and_logs_every_call() -> None:
    log = CallLog()
    with background_server(build_app(log, TOKEN)) as base_url:
        async with (
            httpx2.AsyncClient(headers={"authorization": f"Bearer {TOKEN}"}) as http,
            Client(streamable_http_client(f"{base_url}/mcp", http_client=http)) as client,
        ):
            names = {tool.name for tool in (await client.list_tools()).tools}
            result = await client.call_tool("admin_reset_key", {"key_id": "key-7"})

    assert names == {"get_customer_record", "admin_reset_key", "slow_report"}
    assert result.structured_content == {"key_id": "key-7", "status": "reset"}
    calls = [entry for entry in log.entries() if entry.method == "tools/call"]
    assert [entry.tool for entry in calls] == ["admin_reset_key"]
    assert calls[0].authorization == f"Bearer {TOKEN}"


def test_refuses_a_request_without_the_gateway_credential() -> None:
    log = CallLog()
    with background_server(build_app(log, TOKEN)) as base_url:
        response = httpx.post(f"{base_url}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "x"})
    assert response.status_code == 401
    assert len(log) == 1
    assert log.entries()[0].authorization is None
