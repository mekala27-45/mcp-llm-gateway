"""A mock MCP server on Streamable HTTP with an admin tool and a log of every request."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Any

import anyio
from mcp.server.mcpserver import Context, MCPServer
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


@dataclass(frozen=True)
class Received:
    """One HTTP request as the mock saw it."""

    method: str | None
    tool: str | None
    authorization: str | None
    headers: dict[str, str]


class CallLog:
    """Every request that reached the mock, readable from tests running in another thread."""

    def __init__(self) -> None:
        self._entries: list[Received] = []
        self._lock = threading.Lock()

    def record(self, entry: Received) -> None:
        with self._lock:
            self._entries.append(entry)

    def entries(self) -> list[Received]:
        with self._lock:
            return list(self._entries)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


def _tools() -> MCPServer:
    server = MCPServer("mock-downstream", instructions="Mock server behind the MCP gateway.")

    @server.tool()
    def get_customer_record(customer_id: str) -> dict[str, str]:
        """Look up a customer by id."""
        return {"customer_id": customer_id, "name": "Ada Lindqvist", "tier": "gold"}

    @server.tool()
    def admin_reset_key(key_id: str) -> dict[str, str]:
        """Admin only: rotate an API key."""
        return {"key_id": key_id, "status": "reset"}

    @server.tool()
    async def slow_report(steps: int, ctx: Context) -> str:
        """Build a report slowly, reporting progress after each step."""
        for step in range(1, steps + 1):
            await ctx.report_progress(step, steps)
            await anyio.sleep(0.25)
        return f"report ready after {steps} steps"

    return server


class _Recorder:
    """Logs each request, checks the gateway credential, then hands the request to the MCP app."""

    def __init__(self, app: ASGIApp, log: CallLog, token: str | None) -> None:
        self._app = app
        self._log = log
        self._token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        body = bytearray()
        more = True
        while more:
            message = await receive()
            body.extend(message.get("body", b""))
            more = message.get("more_body", False)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        method, tool = _peek(bytes(body))
        self._log.record(Received(method, tool, headers.get("authorization"), headers))
        if self._token is not None and headers.get("authorization") != f"Bearer {self._token}":
            await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
            return

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self._app(scope, replay, send)


def _peek(body: bytes) -> tuple[str | None, str | None]:
    try:
        decoded: Any = json.loads(body)
    except ValueError:
        return None, None
    if not isinstance(decoded, dict):
        return None, None
    method = decoded.get("method") if isinstance(decoded.get("method"), str) else None
    params = decoded.get("params")
    tool = params.get("name") if isinstance(params, dict) else None
    return method, tool if isinstance(tool, str) else None


def build_app(log: CallLog, token: str | None = None) -> ASGIApp:
    """The mock server app. When token is set, requests without it get 401."""
    return _Recorder(_tools().streamable_http_app(), log, token)
