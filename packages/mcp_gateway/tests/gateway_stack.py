"""Starts the mock downstream with a gateway in front of it, and builds raw MCP requests."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from mcp.shared.inbound import encode_header_value
from mcp.types import (
    CLIENT_CAPABILITIES_META_KEY,
    LATEST_PROTOCOL_VERSION,
    PROTOCOL_VERSION_META_KEY,
)

from gateway_common.serving import background_server
from mcp_gateway.app import create_app
from mcp_gateway.auth import mint_token
from mcp_gateway.settings import Settings
from mock_downstream.app import CallLog, build_app

SECRET = "gateway-test-secret-0123456789abcdef"
DOWNSTREAM_TOKEN = "gateway-to-downstream-credential"
ENVELOPE = {PROTOCOL_VERSION_META_KEY: LATEST_PROTOCOL_VERSION, CLIENT_CAPABILITIES_META_KEY: {}}


@dataclass(frozen=True)
class Stack:
    url: str
    log: CallLog
    settings: Settings

    def token(self, role: str) -> str:
        return mint_token(self.settings, role, f"test-{role}", 300)

    def tool_calls(self) -> list[str | None]:
        return [entry.tool for entry in self.log.entries() if entry.method == "tools/call"]


@contextmanager
def running_stack(downstream_token: str = DOWNSTREAM_TOKEN, **options: Any) -> Iterator[Stack]:
    log = CallLog()
    with background_server(build_app(log, DOWNSTREAM_TOKEN)) as downstream:
        settings = Settings(
            jwt_secret=SECRET,
            downstream_url=f"{downstream}/mcp",
            downstream_token=downstream_token,
            **options,
        )
        with background_server(create_app(settings)) as gateway:
            yield Stack(f"{gateway}/mcp", log, settings)


def rpc(method: str, request_id: int | str = 1, **params: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": {**params, "_meta": ENVELOPE},
    }


def tool_call(name: Any, request_id: int | str = 1, **arguments: Any) -> dict[str, Any]:
    return rpc("tools/call", request_id, name=name, arguments=arguments)


def headers_for(token: str | None, body: dict[str, Any] | None = None) -> dict[str, str]:
    """Headers a well behaved client sends: routing headers that agree with the body."""
    headers = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
        "mcp-protocol-version": LATEST_PROTOCOL_VERSION,
    }
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    if body is not None:
        headers["mcp-method"] = body["method"]
        name = body.get("params", {}).get("name")
        if isinstance(name, str):
            headers["mcp-name"] = encode_header_value(name)
    return headers


def encode(body: Any) -> bytes:
    return json.dumps(body).encode()
