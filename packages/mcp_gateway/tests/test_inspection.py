import json

import pytest
from mcp.shared.inbound import encode_header_value
from mcp.types import HEADER_MISMATCH, INVALID_PARAMS, INVALID_REQUEST, PARSE_ERROR

from mcp_gateway.downstream import filter_listing, rpc_messages
from mcp_gateway.inspection import (
    UNAUTHORIZED_TOOL_CALL,
    Refusal,
    RpcMessage,
    check_routing_headers,
    check_tool_call,
    normalize_tool_name,
    parse_message,
)


@pytest.mark.parametrize(
    ("name", "normalized"),
    [
        ("Admin_reset_key", "admin_reset_key"),
        ("ADMIN_RESET_KEY", "admin_reset_key"),
        (" admin_reset_key", "admin_reset_key"),
        ("\u00a0admin_reset_key", "admin_reset_key"),
        ("admin\u200b_reset_key", "admin_reset_key"),
        ("admin\u00ad_reset_key", "admin_reset_key"),
        ("\ufeffadmin_reset_key", "admin_reset_key"),
        ("\u202eadmin_reset_key", "admin_reset_key"),
        ("\uff41\uff44\uff4d\uff49\uff4e_reset_key", "admin_reset_key"),
        ("\u0430dmin_reset_key", "\u0430dmin_reset_key"),
    ],
    ids=[
        "title case",
        "upper case",
        "leading space",
        "no-break space",
        "zero width space",
        "soft hyphen",
        "byte order mark",
        "right to left override",
        "full width letters",
        "cyrillic a is not folded",
    ],
)
def test_normalization_used_by_the_admin_check(name: str, normalized: str) -> None:
    assert normalize_tool_name(name) == normalized


def refusal(raw: bytes) -> Refusal:
    with pytest.raises(Refusal) as caught:
        parse_message(raw)
    return caught.value


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b'{"jsonrpc": "2.0", "id": 1, "method": "tools/list"', PARSE_ERROR),
        (b'{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"x": NaN}}', PARSE_ERROR),
        (b'{"jsonrpc": "2.0", "id": 1, "id": 2, "method": "tools/list"}', INVALID_REQUEST),
        (b'[{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}]', INVALID_REQUEST),
        (b'"tools/list"', INVALID_REQUEST),
        (b'{"jsonrpc": "2.0", "id": true, "method": "tools/list"}', INVALID_REQUEST),
        (b'{"jsonrpc": "2.0", "id": 1.5, "method": "tools/list"}', INVALID_REQUEST),
        (b'{"jsonrpc": "2.0", "id": null, "method": "tools/list"}', INVALID_REQUEST),
        (b'{"jsonrpc": "1.0", "id": 1, "method": "tools/list"}', INVALID_REQUEST),
        (b'{"jsonrpc": "2.0", "id": 1, "result": {}}', INVALID_REQUEST),
        (b'{"jsonrpc": "2.0", "id": 1, "method": 7}', INVALID_REQUEST),
        (b'{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": [1]}', INVALID_PARAMS),
    ],
    ids=[
        "truncated",
        "NaN",
        "duplicate id",
        "batch",
        "not an object",
        "boolean id",
        "float id",
        "null id",
        "wrong version",
        "a response",
        "method not a string",
        "params not an object",
    ],
)
def test_malformed_bodies_are_refused(raw: bytes, code: int) -> None:
    assert refusal(raw).code == code


def test_a_nested_duplicate_key_is_refused() -> None:
    raw = (
        b'{"jsonrpc": "2.0", "id": 1, "method": "tools/call",'
        b' "params": {"name": "get_customer_record", "name": "admin_reset_key"}}'
    )
    assert refusal(raw).code == INVALID_REQUEST


def message(name: object, **headers: str) -> tuple[list[tuple[str, str]], RpcMessage]:
    body = {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": name}}
    return list(headers.items()), parse_message(json.dumps(body).encode())


def test_mcp_name_is_compared_after_decoding_its_base64_form() -> None:
    header = encode_header_value(" admin_reset_key")
    assert header.startswith("=?base64?")
    pairs, parsed = message(" admin_reset_key", **{"mcp-name": header})
    check_routing_headers(pairs, parsed)


@pytest.mark.parametrize(
    "pairs",
    [
        [("mcp-name", "get_customer_record")],
        [("mcp-method", "tools/list")],
        [("mcp-name", "admin_reset_key"), ("Mcp-Name", "admin_reset_key")],
        [("mcp-name", "=?base64?not base64?=")],
    ],
    ids=["name disagrees", "method disagrees", "name twice", "broken base64"],
)
def test_routing_headers_that_disagree_with_the_body_are_refused(
    pairs: list[tuple[str, str]],
) -> None:
    _, parsed = message("admin_reset_key")
    with pytest.raises(Refusal) as caught:
        check_routing_headers(pairs, parsed)
    assert caught.value.code == HEADER_MISMATCH


def test_only_listed_tools_go_through_when_a_listing_is_given() -> None:
    _, parsed = message("export_all_customers")
    assert check_tool_call(parsed, "admin", None) == "export_all_customers"
    with pytest.raises(Refusal) as caught:
        check_tool_call(parsed, "admin", frozenset({"get_customer_record"}))
    assert caught.value.code == UNAUTHORIZED_TOOL_CALL
    assert caught.value.data == {
        "tool": "export_all_customers",
        "role": "admin",
        "reason": "not listed by the server",
    }


SSE_LISTING = (
    b"event: message\r\n"
    b'data: {"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"get_customer_record"},'
    b'{"name":"ADMIN_reset_key"}]}}\r\n\r\n'
)


def test_a_listing_is_filtered_in_json_and_in_sse_form() -> None:
    json_body = json.dumps(json.loads(SSE_LISTING.split(b"data: ")[1])).encode()
    for body, content_type in ((json_body, "application/json"), (SSE_LISTING, "text/event-stream")):
        filtered = filter_listing(body, content_type, keep=lambda name: "admin" not in name.lower())
        [reply] = rpc_messages(filtered, content_type)
        assert [tool["name"] for tool in reply["result"]["tools"]] == ["get_customer_record"]
