"""Ways a client might try to get an admin tool past the gateway. None of them reach the server."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
import pytest
from gateway_stack import DOWNSTREAM_TOKEN, ENVELOPE, Stack, encode, headers_for, rpc, tool_call
from mcp.types import (
    HEADER_MISMATCH,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
)

from mcp_gateway.inspection import UNAUTHORIZED_TOOL_CALL

TokenMaker = Callable[[Stack], str]


def role(name: str) -> TokenMaker:
    return lambda stack: stack.token(name)


def claims(stack: Stack, **changes: Any) -> dict[str, Any]:
    now = int(time.time())
    return {
        "sub": "mallory",
        "role": "admin",
        "iss": stack.settings.jwt_issuer,
        "aud": stack.settings.jwt_audience,
        "iat": now,
        "exp": now + 300,
        **changes,
    }


def expired(stack: Stack) -> str:
    stale = claims(stack, iat=int(time.time()) - 600, exp=int(time.time()) - 60)
    return jwt.encode(stale, stack.settings.jwt_secret, algorithm="HS256")


def algorithm_none(stack: Stack) -> str:
    def part(value: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'none', 'typ': 'JWT'})}.{part(claims(stack))}."


def another_key(stack: Stack) -> str:
    return jwt.encode(claims(stack), "an-attacker-secret-0123456789abcdef", algorithm="HS256")


VIEWER = role("viewer")
ADMIN_CALL = tool_call("admin_reset_key", key_id="key-1")


def call_named(name: Any) -> bytes:
    return encode(tool_call(name, key_id="key-1"))


def consistent(body: bytes) -> dict[str, str]:
    """Routing headers that agree with the body, as far as the body can be read."""
    try:
        decoded = json.loads(body)
    except ValueError:
        decoded = None
    return headers_for(None, decoded if isinstance(decoded, dict) else None)


@dataclass(frozen=True)
class Attempt:
    name: str
    body: bytes
    status: int
    code: int | None
    token: TokenMaker | None = VIEWER
    headers: dict[str, str] = field(default_factory=dict)
    replace_headers: list[tuple[str, str]] | None = None


DUPLICATE_NAME = (
    b'{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_customer_record",'
    b' "name": "admin_reset_key", "arguments": {"key_id": "key-1"}, "_meta": '
    + json.dumps(ENVELOPE).encode()
    + b"}}"
)

ATTEMPTS = [
    Attempt("Admin_reset_key", call_named("Admin_reset_key"), 403, UNAUTHORIZED_TOOL_CALL),
    Attempt("ADMIN_RESET_KEY", call_named("ADMIN_RESET_KEY"), 403, UNAUTHORIZED_TOOL_CALL),
    Attempt("leading space", call_named(" admin_reset_key"), 403, UNAUTHORIZED_TOOL_CALL),
    Attempt("trailing newline", call_named("admin_reset_key\n"), 403, UNAUTHORIZED_TOOL_CALL),
    Attempt(
        "zero width space after admin",
        call_named("admin\u200b_reset_key"),
        403,
        UNAUTHORIZED_TOOL_CALL,
    ),
    Attempt(
        "full width letters",
        call_named("\uff41\uff44\uff4d\uff49\uff4e_reset_key"),
        403,
        UNAUTHORIZED_TOOL_CALL,
    ),
    Attempt("cyrillic a", call_named("\u0430dmin_reset_key"), 400, INVALID_PARAMS),
    Attempt("duplicate name key", DUPLICATE_NAME, 400, INVALID_REQUEST),
    Attempt("name as a number", call_named(7), 400, INVALID_PARAMS),
    Attempt("name as a list", call_named(["admin_reset_key"]), 400, INVALID_PARAMS),
    Attempt("name as an object", call_named({"name": "admin_reset_key"}), 400, INVALID_PARAMS),
    Attempt(
        "batch carrying the admin call",
        encode([rpc("tools/list"), ADMIN_CALL]),
        400,
        INVALID_REQUEST,
    ),
    Attempt(
        "method in a different case",
        encode({**ADMIN_CALL, "method": "Tools/Call"}),
        404,
        METHOD_NOT_FOUND,
    ),
    Attempt(
        "Mcp-Name disagrees with the body",
        encode(ADMIN_CALL),
        400,
        HEADER_MISMATCH,
        headers={"mcp-name": "get_customer_record"},
    ),
    Attempt(
        "Mcp-Method disagrees with the body",
        encode(ADMIN_CALL),
        400,
        HEADER_MISMATCH,
        headers={"mcp-method": "tools/list"},
    ),
    Attempt("expired token", encode(ADMIN_CALL), 401, None, token=expired),
    Attempt("token with algorithm none", encode(ADMIN_CALL), 401, None, token=algorithm_none),
    Attempt("token signed with another key", encode(ADMIN_CALL), 401, None, token=another_key),
    Attempt("no token", encode(ADMIN_CALL), 401, None, token=None),
    Attempt(
        "two Authorization headers",
        encode(ADMIN_CALL),
        401,
        None,
        token=None,
        replace_headers=[("authorization", "viewer"), ("authorization", "admin")],
    ),
    Attempt(
        "role smuggled in X-Forwarded-User",
        encode(ADMIN_CALL),
        403,
        UNAUTHORIZED_TOOL_CALL,
        headers={"x-forwarded-user": "admin", "x-role": "admin"},
    ),
    Attempt(
        "body over the size limit",
        encode(tool_call("get_customer_record", customer_id="C" * 70_000)),
        413,
        INVALID_REQUEST,
    ),
    Attempt("NaN in the body", call_named("x").replace(b'"x"', b"NaN"), 400, PARSE_ERROR),
    Attempt(
        "legacy initialize handshake",
        encode(rpc("initialize", protocolVersion="2025-11-25", capabilities={})),
        404,
        METHOD_NOT_FOUND,
    ),
    Attempt(
        "unknown notification",
        encode({"jsonrpc": "2.0", "method": "notifications/roots/list_changed"}),
        404,
        METHOD_NOT_FOUND,
    ),
]


def send(stack: Stack, attempt: Attempt) -> httpx.Response:
    headers = list({**consistent(attempt.body), **attempt.headers}.items())
    if attempt.token is not None:
        headers.append(("authorization", f"Bearer {attempt.token(stack)}"))
    if attempt.replace_headers is not None:
        tokens = {"viewer": stack.token("viewer"), "admin": stack.token("admin")}
        headers += [(name, f"Bearer {tokens[value]}") for name, value in attempt.replace_headers]
    return httpx.post(stack.url, content=attempt.body, headers=headers)


@pytest.mark.parametrize("attempt", ATTEMPTS, ids=lambda attempt: attempt.name)
def test_attempt_is_refused_and_never_reaches_the_server(stack: Stack, attempt: Attempt) -> None:
    before = len(stack.log)
    response = send(stack, attempt)

    assert response.status_code == attempt.status
    if attempt.code is not None:
        assert response.json()["error"]["code"] == attempt.code
    assert len(stack.log) == before


def test_the_refused_admin_call_names_the_tool_and_role(stack: Stack) -> None:
    attempt = ATTEMPTS[0]
    error = send(stack, attempt).json()["error"]
    assert error["message"] == "Unauthorized Tool Call"
    assert error["data"] == {"tool": "admin_reset_key", "role": "viewer"}


def test_the_downstream_never_sees_the_client_token(stack: Stack) -> None:
    tokens = [stack.token("viewer"), stack.token("admin")]
    for token in tokens:
        body = tool_call("get_customer_record", customer_id="CUST-10001")
        httpx.post(
            stack.url, json=body, headers={**headers_for(token, body), "x-forwarded-user": "x"}
        )
    for entry in stack.log.entries():
        assert entry.authorization == f"Bearer {DOWNSTREAM_TOKEN}"
        assert not any(token in value for token in tokens for value in entry.headers.values())
        assert "x-forwarded-user" not in entry.headers
