"""Talks to the downstream MCP server: forwards requests and reads its tool listing."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import Any

import httpx
from mcp.types import (
    CLIENT_CAPABILITIES_META_KEY,
    LATEST_PROTOCOL_VERSION,
    PROTOCOL_VERSION_META_KEY,
)

from gateway_common.sse import SseParser

# The client's Authorization header is not on this list and never goes downstream.
FORWARDED_REQUEST_HEADERS = frozenset({"accept", "mcp-protocol-version", "mcp-method", "mcp-name"})
FORWARDED_RESPONSE_HEADERS = frozenset({"content-type", "cache-control"})
MAX_BUFFERED_BYTES = 1 << 20
_LISTING_PAGES = 20


def rpc_messages(body: bytes, content_type: str) -> list[dict[str, Any]]:
    """The JSON-RPC messages in a downstream reply, which may be JSON or an SSE stream."""
    if content_type.startswith("text/event-stream"):
        parser = SseParser()
        events = parser.feed(body) + parser.close()
        texts: Iterable[str] = (event.data for event in events if event.data is not None)
    else:
        texts = [body.decode()]
    messages = []
    for text in texts:
        decoded = json.loads(text)
        if isinstance(decoded, dict):
            messages.append(decoded)
    return messages


def filter_listing(body: bytes, content_type: str, keep: Callable[[str], bool]) -> bytes:
    """Rewrite a tools/list reply so it only lists the tools keep() allows."""

    def rewrite(message: dict[str, Any]) -> dict[str, Any]:
        result = message.get("result")
        if isinstance(result, dict) and isinstance(result.get("tools"), list):
            tools = [t for t in result["tools"] if isinstance(t, dict) and keep(str(t.get("name")))]
            message = {**message, "result": {**result, "tools": tools}}
        return message

    if not content_type.startswith("text/event-stream"):
        return json.dumps(rewrite(json.loads(body))).encode()
    parser = SseParser()
    out = bytearray()
    for event in parser.feed(body) + parser.close():
        if event.data is not None and event.data.startswith("{"):
            event = event.with_data(json.dumps(rewrite(json.loads(event.data))))
        out.extend(event.encode())
    return bytes(out)


class Downstream:
    def __init__(self, url: str, token: str | None, client: httpx.AsyncClient) -> None:
        self._url = url
        self._token = token
        self._client = client

    def _credential(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self._token}"} if self._token else {}

    async def open(
        self, body: bytes, client_headers: Iterable[tuple[str, str]], request_id: str
    ) -> httpx.Response:
        """Send one checked request and return the reply unread, so it can stream back."""
        headers = {
            name: value
            for name, value in client_headers
            if name in FORWARDED_REQUEST_HEADERS or name.startswith("mcp-param-")
        }
        headers.update(
            {
                "content-type": "application/json",
                "accept-encoding": "identity",
                "x-request-id": request_id,
            }
        )
        headers.update(self._credential())
        request = self._client.build_request("POST", self._url, content=body, headers=headers)
        return await self._client.send(request, stream=True)

    async def tool_names(self) -> frozenset[str]:
        """The tool names the downstream lists, asked with the gateway's own credential."""
        names: set[str] = set()
        cursor: str | None = None
        envelope = {
            PROTOCOL_VERSION_META_KEY: LATEST_PROTOCOL_VERSION,
            CLIENT_CAPABILITIES_META_KEY: {},
        }
        for page in range(_LISTING_PAGES):
            params: dict[str, Any] = {"_meta": envelope}
            if cursor is not None:
                params["cursor"] = cursor
            request = {
                "jsonrpc": "2.0",
                "id": f"gateway-list-{page}",
                "method": "tools/list",
                "params": params,
            }
            headers = {
                "accept": "application/json, text/event-stream",
                "mcp-protocol-version": LATEST_PROTOCOL_VERSION,
                "mcp-method": "tools/list",
                **self._credential(),
            }
            reply = await self._client.post(self._url, json=request, headers=headers)
            reply.raise_for_status()
            messages = rpc_messages(reply.content, reply.headers.get("content-type", ""))
            empty: dict[str, Any] = {}
            result = next(
                (m["result"] for m in messages if isinstance(m.get("result"), dict)), empty
            )
            names.update(
                t["name"] for t in result.get("tools", []) if isinstance(t.get("name"), str)
            )
            cursor = result.get("nextCursor")
            if not isinstance(cursor, str):
                break
        return frozenset(names)
