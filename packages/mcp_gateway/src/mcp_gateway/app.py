"""The MCP security gateway: authenticate, inspect the body, then forward or refuse."""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from mcp.shared.inbound import ERROR_CODE_HTTP_STATUS
from mcp.types import INTERNAL_ERROR, INVALID_REQUEST
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from gateway_common.logs import log_event, request_id
from mcp_gateway.auth import Principal, Unauthenticated, authenticate
from mcp_gateway.downstream import (
    FORWARDED_RESPONSE_HEADERS,
    MAX_BUFFERED_BYTES,
    Downstream,
    filter_listing,
)
from mcp_gateway.inspection import (
    ADMIN_PREFIX,
    UNAUTHORIZED_TOOL_CALL,
    Refusal,
    RequestId,
    RpcMessage,
    check_method,
    check_routing_headers,
    check_tool_call,
    normalize_tool_name,
    parse_message,
)
from mcp_gateway.settings import Settings

logger = logging.getLogger("mcp_gateway")
audit_logger = logging.getLogger("mcp_gateway.audit")

HTTP_STATUS = {**ERROR_CODE_HTTP_STATUS, UNAUTHORIZED_TOOL_CALL: 403, INTERNAL_ERROR: 502}
LISTING_TTL_SECONDS = 30.0


class _TooLarge(Exception):
    pass


def rpc_error(
    reply_id: RequestId | None,
    code: int,
    message: str,
    data: dict[str, Any] | None = None,
    status: int | None = None,
) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    body = {"jsonrpc": "2.0", "id": reply_id, "error": error}
    return JSONResponse(body, status_code=status or HTTP_STATUS.get(code, 400))


async def _read_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise _TooLarge
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise _TooLarge
    return bytes(body)


async def _relay(reply: httpx.Response) -> AsyncIterator[bytes]:
    try:
        async for chunk in reply.aiter_raw():
            yield chunk
    finally:
        await reply.aclose()


def _is_rpc_error(body: bytes) -> bool:
    try:
        decoded = json.loads(body)
    except ValueError:
        return False
    error = decoded.get("error") if isinstance(decoded, dict) else None
    return (
        isinstance(error, dict)
        and decoded.get("jsonrpc") == "2.0"
        and isinstance(error.get("code"), int)
        and isinstance(error.get("message"), str)
    )


class Gateway:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client
        self._downstream: Downstream | None = None
        self._listing: tuple[float, frozenset[str]] | None = None

    @asynccontextmanager
    async def lifespan(self, app: Starlette) -> AsyncIterator[None]:
        client = self._client or httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=300.0))
        self._downstream = Downstream(
            self._settings.downstream_url, self._settings.downstream_token, client
        )
        try:
            yield
        finally:
            if self._client is None:
                await client.aclose()

    async def handle(self, request: Request) -> Response:
        token = request_id.set(uuid.uuid4().hex[:16])
        try:
            return await self._handle(request)
        except Exception:
            logger.exception("gateway failure")
            return rpc_error(None, INTERNAL_ERROR, "Internal error", status=500)
        finally:
            request_id.reset(token)

    async def _handle(self, request: Request) -> Response:
        try:
            principal = authenticate(request.headers.getlist("authorization"), self._settings)
        except Unauthenticated as exc:
            self._audit(
                "unauthenticated", None, None, fingerprint=exc.fingerprint, reason=exc.reason
            )
            challenge = 'Bearer realm="mcp-gateway"'
            if exc.fingerprint is not None:
                challenge += ', error="invalid_token"'
            return JSONResponse(
                {"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": challenge}
            )
        try:
            raw = await _read_body(request, self._settings.max_body_bytes)
        except _TooLarge:
            self._audit("rejected", principal, None, code=INVALID_REQUEST, reason="body too large")
            return rpc_error(None, INVALID_REQUEST, "Request body too large", status=413)

        message: RpcMessage | None = None
        try:
            message = parse_message(raw)
            headers = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in request.headers.raw]
            check_routing_headers(headers, message)
            check_method(message)
            if message.method == "tools/call":
                listed = (
                    await self._listed_tools(message) if self._settings.listed_tools_only else None
                )
                check_tool_call(message, principal.role, listed)
        except Refusal as refusal:
            decision = "denied" if refusal.code == UNAUTHORIZED_TOOL_CALL else "rejected"
            self._audit(decision, principal, message, code=refusal.code)
            return rpc_error(refusal.request_id, refusal.code, refusal.message, refusal.data)

        self._audit("forwarded", principal, message)
        return await self._forward(request, message, principal)

    async def _listed_tools(self, message: RpcMessage) -> frozenset[str]:
        now = time.monotonic()
        if self._listing is None or now - self._listing[0] > LISTING_TTL_SECONDS:
            assert self._downstream is not None
            try:
                self._listing = (now, await self._downstream.tool_names())
            except (httpx.HTTPError, ValueError):
                logger.warning("could not read the downstream tool listing", exc_info=True)
                raise Refusal(
                    INTERNAL_ERROR, "Tool listing unavailable", message.request_id
                ) from None
        return self._listing[1]

    async def _forward(
        self, request: Request, message: RpcMessage, principal: Principal
    ) -> Response:
        assert self._downstream is not None
        # The checked object is sent, not the client's bytes, so the downstream cannot
        # parse anything other than what was authorized.
        body = json.dumps(message.body, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        try:
            reply = await self._downstream.open(
                body.encode(), request.headers.items(), request_id.get() or ""
            )
        except httpx.HTTPError as exc:
            log_event(
                logger, "downstream unreachable", level=logging.WARNING, error=type(exc).__name__
            )
            return rpc_error(message.request_id, INTERNAL_ERROR, "Downstream server unavailable")

        if message.is_notification:
            await reply.aclose()
            if reply.is_success:
                return Response(status_code=202)
            return rpc_error(None, INTERNAL_ERROR, "Downstream server refused the notification")

        content_type = reply.headers.get("content-type", "")
        if reply.status_code >= 400:
            return await self._sanitized(reply, message.request_id, content_type)

        hide = self._settings.hide_admin_tools and principal.role != "admin"
        if hide and message.method == "tools/list":
            try:
                filtered = filter_listing(
                    await self._read_capped(reply),
                    content_type,
                    keep=lambda name: not normalize_tool_name(name).startswith(ADMIN_PREFIX),
                )
            except (_TooLarge, ValueError):
                return rpc_error(message.request_id, INTERNAL_ERROR, "Downstream reply unreadable")
            return Response(filtered, status_code=reply.status_code, media_type=content_type)

        passthrough = {k: v for k, v in reply.headers.items() if k in FORWARDED_RESPONSE_HEADERS}
        return StreamingResponse(_relay(reply), status_code=reply.status_code, headers=passthrough)

    async def _read_capped(self, reply: httpx.Response) -> bytes:
        body = bytearray()
        try:
            async for chunk in reply.aiter_raw():
                body.extend(chunk)
                if len(body) > MAX_BUFFERED_BYTES:
                    raise _TooLarge
        finally:
            await reply.aclose()
        return bytes(body)

    async def _sanitized(
        self, reply: httpx.Response, reply_id: RequestId | None, content_type: str
    ) -> Response:
        """Pass a JSON-RPC error through; replace anything else, so no upstream detail leaks."""
        try:
            body = await self._read_capped(reply)
        except _TooLarge:
            body = b""
        if content_type.startswith("application/json") and _is_rpc_error(body):
            return Response(body, status_code=reply.status_code, media_type="application/json")
        log_event(logger, "downstream error", level=logging.WARNING, status=reply.status_code)
        return rpc_error(reply_id, INTERNAL_ERROR, "Downstream server error")

    def _audit(
        self,
        decision: str,
        principal: Principal | None,
        message: RpcMessage | None,
        fingerprint: str | None = None,
        code: int | None = None,
        reason: str | None = None,
    ) -> None:
        tool = message.params.get("name") if message and message.method == "tools/call" else None
        log_event(
            audit_logger,
            "decision",
            decision=decision,
            token_fp=principal.fingerprint if principal else fingerprint,
            role=principal.role if principal else None,
            method=message.method[:64] if message else None,
            tool=tool[:128] if isinstance(tool, str) else None,
            code=code,
            reason=reason,
        )


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def create_app(settings: Settings, client: httpx.AsyncClient | None = None) -> Starlette:
    """The gateway app. Pass client to share one connection pool, for example in tests."""
    gateway = Gateway(settings, client)
    return Starlette(
        routes=[Route("/mcp", gateway.handle, methods=["POST"]), Route("/healthz", health)],
        lifespan=gateway.lifespan,
    )
