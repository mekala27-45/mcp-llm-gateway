"""The LLM gateway: one OpenAI compatible endpoint in front of a provider."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from gateway_common.logs import request_id
from llm_gateway.errors import INTERNAL, UPSTREAM, GatewayError, error_response
from llm_gateway.providers import Provider
from llm_gateway.settings import Settings

logger = logging.getLogger("llm_gateway")


async def _read_json(request: Request, limit: int) -> dict[str, Any]:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise GatewayError(
                "request_too_large", "body_too_large", f"The body is over {limit} bytes."
            )
    try:
        decoded = json.loads(body)
    except ValueError:
        raise GatewayError(
            "invalid_request", "invalid_json", "The body is not valid JSON."
        ) from None
    if not isinstance(decoded, dict):
        raise GatewayError("invalid_request", "invalid_json", "The body must be a JSON object.")
    messages = decoded.get("messages")
    if not isinstance(messages, list) or not messages:
        raise GatewayError(
            "invalid_request", "invalid_messages", "messages must be a non-empty list."
        )
    return decoded


async def _relay(reply: httpx.Response) -> AsyncIterator[bytes]:
    try:
        async for chunk in reply.aiter_raw():
            yield chunk
    finally:
        await reply.aclose()


class LlmGateway:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client
        self._primary: Provider | None = None

    @asynccontextmanager
    async def lifespan(self, app: Starlette) -> AsyncIterator[None]:
        client = self._client or httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=300.0))
        self._primary = Provider(self._settings.primary, client)
        try:
            yield
        finally:
            if self._client is None:
                await client.aclose()

    async def chat_completions(self, request: Request) -> Response:
        rid = uuid.uuid4().hex[:16]
        token = request_id.set(rid)
        try:
            body = await _read_json(request, self._settings.max_body_bytes)
            return await self._complete(body, rid)
        except GatewayError as error:
            return error_response(error, rid)
        except Exception:
            logger.exception("request failed")
            return error_response(INTERNAL, rid)
        finally:
            request_id.reset(token)

    async def _complete(self, body: dict[str, Any], rid: str) -> Response:
        assert self._primary is not None
        try:
            reply = await self._primary.open(body)
        except httpx.HTTPError:
            raise UPSTREAM from None
        if reply.status_code >= 400:
            await reply.aclose()
            raise UPSTREAM
        headers = {"x-request-id": rid}
        if body.get("stream") is True:
            return StreamingResponse(_relay(reply), media_type="text/event-stream", headers=headers)
        payload = json.loads(await reply.aread())
        return JSONResponse(payload, headers=headers)


async def _http_error(request: Request, exc: Exception) -> Response:
    status = exc.status_code if isinstance(exc, HTTPException) else 500
    kind = "invalid_request" if status < 500 else "internal_error"
    error = GatewayError(kind, f"http_{status}", "No such endpoint or method.")
    response = error_response(error, uuid.uuid4().hex[:16])
    response.status_code = status
    return response


def create_app(settings: Settings, client: httpx.AsyncClient | None = None) -> Starlette:
    gateway = LlmGateway(settings, client)
    return Starlette(
        routes=[Route("/v1/chat/completions", gateway.chat_completions, methods=["POST"])],
        lifespan=gateway.lifespan,
        exception_handlers={HTTPException: _http_error},
    )
