import httpx
from gateway_stack import Stack, headers_for, rpc, running_stack, tool_call

from mcp_gateway.inspection import UNAUTHORIZED_TOOL_CALL


def listed_names(stack: Stack, role: str) -> set[str]:
    body = rpc("tools/list")
    response = httpx.post(stack.url, json=body, headers=headers_for(stack.token(role), body))
    return {tool["name"] for tool in response.json()["result"]["tools"]}


def test_policies_are_off_by_default(stack: Stack) -> None:
    assert "admin_reset_key" in listed_names(stack, "viewer")


def test_hide_admin_tools_removes_them_from_a_viewer_listing_only() -> None:
    with running_stack(hide_admin_tools=True) as stack:
        assert listed_names(stack, "viewer") == {"get_customer_record", "slow_report"}
        assert "admin_reset_key" in listed_names(stack, "admin")


def test_listed_tools_only_refuses_a_name_the_server_does_not_list() -> None:
    with running_stack(listed_tools_only=True) as stack:
        body = tool_call("export_all_customers")
        refused = httpx.post(stack.url, json=body, headers=headers_for(stack.token("admin"), body))
        body = tool_call("get_customer_record", customer_id="CUST-10001")
        allowed = httpx.post(stack.url, json=body, headers=headers_for(stack.token("admin"), body))
        calls = stack.tool_calls()

    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == UNAUTHORIZED_TOOL_CALL
    assert refused.json()["error"]["data"]["reason"] == "not listed by the server"
    assert allowed.status_code == 200
    assert calls == ["get_customer_record"]


def test_without_the_policy_an_unlisted_name_reaches_the_server(stack: Stack) -> None:
    before = len(stack.tool_calls())
    body = tool_call("export_all_customers")
    httpx.post(stack.url, json=body, headers=headers_for(stack.token("admin"), body))
    assert stack.tool_calls()[before:] == ["export_all_customers"]
