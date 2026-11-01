"""The LLM gateway: one OpenAI compatible endpoint that authenticates the tenant, rate limits it
by tokens, routes to the primary or the backup provider, and redacts PII from the reply."""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections import Counter
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from typing import Any

import anyio
import httpx
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from gateway_common.logs import log_event, request_id
from gateway_common.sse import EventTooLarge
from llm_gateway.db import Database
from llm_gateway.errors import (
    GatewayError,
    error_event,
    error_response,
    internal_error,
    upstream_unavailable,
)
from llm_gateway.estimate import TokenEstimator
from llm_gateway.limiter import (
    Admission,
    EstimateTooLarge,
    RateLimited,
    SlidingWindowLimiter,
    wall_clock_ms,
)
from llm_gateway.providers import Provider
from llm_gateway.router import NoProvider, ProviderRejected, Routed, Router
from llm_gateway.settings import Settings
from llm_gateway.stream import ChunkRewriter, UpstreamStreamError, redact_completion, rewrite_stream
from llm_gateway.tenants import TenantStore

logger = logging.getLogger("llm_gateway")


@dataclass
class Exchange:
    """One request as the request log keeps it: who, which route, what happened. No content."""

    request_id: str
    tenant_id: str
    created_ms: int
    started: float = field(default_factory=time.perf_counter)
    prompt_tokens: int = 0
    admission: Admission | None = None
    route: str = "none"
    attempts: str = ""
    outcome: str = ""
    tokens: int | None = None


async def _read_json(request: Request, limit: int) -> dict[str, Any]:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise GatewayError(
                "request_too_large", "body_too_large", f"The body is over {limit:,} bytes."
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


def _reported_total(usage: Any) -> int | None:
    total = usage.get("total_tokens") if isinstance(usage, dict) else None
    return total if type(total) is int and total >= 0 else None


class _Stream(StreamingResponse):
    """A streamed reply whose cleanup runs exactly once, whether the stream finished, broke
    upstream, or lost its client before the first byte or in the middle."""

    def __init__(
        self,
        relay: AsyncGenerator[bytes, None],
        cleanup: Callable[[], Awaitable[None]],
        headers: dict[str, str],
    ) -> None:
        super().__init__(relay, media_type="text/event-stream", headers=headers)
        self._cleanup = cleanup

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Shielded, so a request cancelled under it (a server shutting down, say) still
            # settles its reservation and closes the provider connection.
            with anyio.CancelScope(shield=True):
                await self._cleanup()


class LlmGateway:
    _db: Database
    _tenants: TenantStore
    _limiter: SlidingWindowLimiter
    _router: Router

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], int] = wall_clock_ms,
    ) -> None:
        self._settings = settings
        self._client = client
        self._clock = clock
        self._estimator = TokenEstimator()

    @asynccontextmanager
    async def lifespan(self, app: Starlette) -> AsyncIterator[None]:
        self._db = await Database.open(self._settings.db_path)
        self._tenants = TenantStore(self._db, self._settings.secret)
        self._limiter = SlidingWindowLimiter(
            self._db, self._settings.tokens_per_minute, self._clock
        )
        client = self._client or httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=300.0))
        backup = self._settings.backup
        self._router = Router(
            Provider(self._settings.primary, client),
            Provider(backup, client) if backup else None,
        )
        log_event(logger, "ready", token_estimates=self._estimator.method)
        try:
            yield
        finally:
            if self._client is None:
                await client.aclose()
            await self._db.close()

    async def chat_completions(self, request: Request) -> Response:
        rid = request.state.request_id = uuid.uuid4().hex[:16]
        request_id.set(rid)
        exchange: Exchange | None = None
        try:
            tenant = await self._authenticate(request.headers.getlist("authorization"))
            exchange = Exchange(rid, tenant, self._clock())
            body = await _read_json(request, self._settings.max_body_bytes)
            return await self._serve(body, exchange)
        except GatewayError as refused:
            await self._fail(exchange, refused)
            raise
        except Exception as exc:
            logger.exception("request failed")
            failed = internal_error()
            await self._fail(exchange, failed)
            raise failed from exc

    async def _authenticate(self, values: list[str]) -> str:
        """The tenant that owns the one Bearer key in the request."""
        scheme, _, key = values[0].partition(" ") if len(values) == 1 else ("", "", "")
        key = key.strip()
        tenant = await self._tenants.find(key) if scheme.lower() == "bearer" and key else None
        if tenant is None:
            raise GatewayError(
                "unauthorized",
                "invalid_api_key",
                "Send one Authorization: Bearer header with a valid API key.",
            )
        return tenant

    def _completion_bound(self, body: dict[str, Any]) -> int:
        """The most tokens the reply may use. A request without a bound is given the default,
        and the provider is sent it too, so every reservation is finite."""
        bounds = [body[key] for key in ("max_tokens", "max_completion_tokens") if key in body]
        if not bounds:
            body["max_tokens"] = self._settings.default_max_tokens
            bounds = [body["max_tokens"]]
        for bound in bounds:
            if type(bound) is not int or bound < 1:
                raise GatewayError(
                    "invalid_request",
                    "invalid_max_tokens",
                    "max_tokens must be a whole number above zero.",
                )
        choices = body.get("n", 1)
        if type(choices) is not int or choices < 1:
            raise GatewayError(
                "invalid_request", "invalid_n", "n must be a whole number above zero."
            )
        # max_tokens applies to each completion; every requested choice consumes budget.
        return int(max(bounds)) * choices

    async def _serve(self, body: dict[str, Any], exchange: Exchange) -> Response:
        stream = body.get("stream") is True
        exchange.prompt_tokens = self._estimator.prompt(body["messages"])
        estimate = exchange.prompt_tokens + self._completion_bound(body)
        forward_usage = True
        if stream:
            options = body.get("stream_options")
            options = options if isinstance(options, dict) else {}
            # Usage is always asked for, so the reservation can be reconciled to it. The client
            # sees the usage chunk only if it asked for one.
            forward_usage = options.get("include_usage") is True
            body["stream_options"] = {**options, "include_usage": True}
        await self._admit(exchange, estimate)
        routed = await self._route(body, stream, exchange)
        headers = {"x-request-id": exchange.request_id}
        if not stream:
            return await self._reply(routed, exchange, headers)
        rewriter = ChunkRewriter(self._estimator.count, forward_usage)
        relay = self._relay(routed, rewriter, exchange)
        exchange.outcome = "client_closed"
        cleanup = partial(self._close_stream, relay, routed, rewriter, exchange)
        return _Stream(relay, cleanup, headers)

    async def _admit(self, exchange: Exchange, estimate: int) -> None:
        limit = self._limiter.limit
        try:
            exchange.admission = await self._limiter.admit(
                exchange.tenant_id, exchange.request_id, estimate
            )
        except EstimateTooLarge:
            raise GatewayError(
                "request_too_large",
                "estimate_over_limit",
                f"This request may use up to {estimate:,} tokens, more than the {limit:,} "
                "a key may use in a minute. Lower max_tokens or shorten the prompt.",
            ) from None
        except RateLimited as limited:
            raise GatewayError(
                "rate_limited",
                "tokens_per_minute",
                f"This request would take the key over {limit:,} tokens in a minute. "
                f"Retry in {limited.retry_after_s} seconds.",
                retry_after=limited.retry_after_s,
            ) from None

    async def _route(self, body: dict[str, Any], stream: bool, exchange: Exchange) -> Routed:
        try:
            routed = await self._router.route(body, stream)
        except ProviderRejected as rejected:
            exchange.attempts = ",".join(rejected.attempts)
            raise GatewayError(
                "invalid_request",
                "invalid_parameters",
                "This request was refused as invalid. Check its parameters.",
            ) from None
        except NoProvider as failed:
            exchange.attempts = ",".join(failed.attempts)
            raise upstream_unavailable() from None
        exchange.route = routed.provider.name
        exchange.attempts = ",".join(routed.attempts)
        return routed

    async def _reply(self, routed: Routed, exchange: Exchange, headers: dict[str, str]) -> Response:
        try:
            payload = json.loads(routed.payload)
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            raise upstream_unavailable()
        counts: Counter[str] = Counter()
        generated = redact_completion(payload, counts, self._estimator.count)
        reported = _reported_total(payload.get("usage"))
        used = reported if reported is not None else exchange.prompt_tokens + generated
        await self._settle(exchange, used)
        exchange.outcome = "ok"
        await self._finish(exchange, 200, redactions=dict(counts))
        return JSONResponse(payload, headers=headers)

    async def _relay(
        self, routed: Routed, rewriter: ChunkRewriter, exchange: Exchange
    ) -> AsyncGenerator[bytes, None]:
        assert routed.body is not None
        try:
            async for piece in rewrite_stream(routed.body, rewriter):
                yield piece
            exchange.outcome = "ok"
        except (httpx.HTTPError, UpstreamStreamError, EventTooLarge):
            # Part of the answer may already be with the client, so a stream that breaks is
            # not retried on the backup. It ends here with one error event.
            exchange.outcome = "stream_broke"
            yield error_event(upstream_unavailable(), exchange.request_id)
        except Exception:
            exchange.outcome = "internal_error"
            logger.exception("stream failed")
            yield error_event(internal_error(), exchange.request_id)

    async def _close_stream(
        self,
        relay: AsyncGenerator[bytes, None],
        routed: Routed,
        rewriter: ChunkRewriter,
        exchange: Exchange,
    ) -> None:
        await relay.aclose()
        await routed.reply.aclose()
        reported = _reported_total(rewriter.usage)
        counted = exchange.prompt_tokens + rewriter.completion_tokens
        await self._settle(exchange, reported if reported is not None else counted)
        await self._finish(
            exchange, 200, redactions=dict(rewriter.counts), largest_hold=rewriter.largest_hold
        )

    async def _settle(self, exchange: Exchange, used: int | None) -> None:
        """Reconcile the reservation to what the request used, or release it if it used none.
        The admission is taken off the exchange, so a second call does nothing."""
        admission, exchange.admission = exchange.admission, None
        if admission is None:
            return
        exchange.tokens = used
        if used is None:
            await self._limiter.release(admission.request_id)
        else:
            await self._limiter.reconcile(admission.request_id, used)

    async def _fail(self, exchange: Exchange | None, error: GatewayError) -> None:
        if exchange is None:
            log_event(logger, "refused", outcome=error.kind, status=error.status)
            return
        exchange.outcome = error.kind
        await self._settle(exchange, None)
        await self._finish(exchange, error.status)

    async def _finish(self, exchange: Exchange, status: int, **fields: Any) -> None:
        """One request log row and one log line for every request. Never any content."""
        latency_ms = round((time.perf_counter() - exchange.started) * 1000)
        await self._db.execute(
            "INSERT INTO request_log (request_id, tenant_id, created_ms, route, attempts,"
            " outcome, status, latency_ms, tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                exchange.request_id,
                exchange.tenant_id,
                exchange.created_ms,
                exchange.route,
                exchange.attempts,
                exchange.outcome,
                status,
                latency_ms,
                exchange.tokens,
            ),
        )
        log_event(
            logger,
            "request",
            tenant=exchange.tenant_id,
            route=exchange.route,
            attempts=exchange.attempts,
            outcome=exchange.outcome,
            status=status,
            latency_ms=latency_ms,
            tokens=exchange.tokens,
            **fields,
        )


async def _handle(request: Request, exc: Exception) -> Response:
    """The one exception handler. The endpoint has already turned anything that is not a
    GatewayError into a generic internal error, so no exception text gets this far."""
    rid = getattr(request.state, "request_id", None) or uuid.uuid4().hex[:16]
    if isinstance(exc, HTTPException):
        error = GatewayError("invalid_request", "unknown_route", "No such endpoint or method.")
        response = error_response(error, rid)
        response.status_code = exc.status_code
        return response
    return error_response(exc if isinstance(exc, GatewayError) else internal_error(), rid)


def create_app(
    settings: Settings,
    client: httpx.AsyncClient | None = None,
    clock: Callable[[], int] = wall_clock_ms,
) -> Starlette:
    gateway = LlmGateway(settings, client, clock)
    return Starlette(
        routes=[Route("/v1/chat/completions", gateway.chat_completions, methods=["POST"])],
        lifespan=gateway.lifespan,
        exception_handlers={GatewayError: _handle, HTTPException: _handle},
    )
