"""Primary first, then the backup. route() hands back one reply or raises. A reply that comes
after its provider was given up on is closed with its connection and never read."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from gateway_common.logs import log_event
from llm_gateway.providers import Provider

logger = logging.getLogger("llm_gateway.router")

# Statuses that say the request itself is wrong, so the backup would refuse it too.
CLIENT_ERRORS = frozenset({400, 413, 422})


class NoProvider(Exception):
    """Every provider failed, timed out or was rate limited."""

    def __init__(self, attempts: list[str]) -> None:
        super().__init__(", ".join(attempts))
        self.attempts = attempts


class ProviderRejected(Exception):
    """A provider refused the request as invalid."""

    def __init__(self, attempts: list[str]) -> None:
        super().__init__(", ".join(attempts))
        self.attempts = attempts


@dataclass
class Routed:
    """The one reply that will reach the client: a stream that starts with its first chunk,
    or a whole body."""

    provider: Provider
    reply: httpx.Response
    attempts: list[str]
    body: AsyncIterator[bytes] | None = None
    payload: bytes = b""


async def _starting_with(first: bytes, rest: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    yield first
    async for chunk in rest:
        yield chunk


def _seconds(value: str | None) -> float | None:
    try:
        return float(value) if value else None
    except ValueError:
        return None


async def _close(reply: httpx.Response | None) -> None:
    if reply is not None:
        await reply.aclose()


class Router:
    def __init__(
        self,
        primary: Provider,
        backup: Provider | None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._providers = [p for p in (primary, backup) if p is not None]
        self._clock = clock
        self._cooling_until: dict[str, float] = {}

    def _order(self) -> list[Provider]:
        """The configured order, with a provider that is cooling down after a 429 moved last.
        It is still tried when nothing else answers."""
        now = self._clock()
        return sorted(self._providers, key=lambda p: self._cooling_until.get(p.name, 0.0) > now)

    async def route(self, body: dict[str, Any], stream: bool) -> Routed:
        attempts: list[str] = []
        for provider in self._order():
            outcome, routed = await self._try(provider, body, stream)
            attempts.append(f"{provider.name}:{outcome}")
            if routed is not None:
                routed.attempts = attempts
                return routed
            log_event(
                logger,
                "provider failed",
                level=logging.WARNING,
                provider=provider.name,
                outcome=outcome,
            )
            if outcome.startswith("rejected"):
                raise ProviderRejected(attempts)
        raise NoProvider(attempts)

    async def _try(
        self, provider: Provider, body: dict[str, Any], stream: bool
    ) -> tuple[str, Routed | None]:
        reply: httpx.Response | None = None
        try:
            # For a stream the timeout runs to the first byte of the body, because headers
            # alone say nothing about whether tokens are coming. Otherwise it covers the whole
            # reply. When it fires, the request is cancelled and its connection closed.
            async with asyncio.timeout(provider.config.timeout_ms / 1000):
                reply = await provider.open(body)
                if reply.status_code == 200 and stream:
                    chunks = reply.aiter_raw()
                    first = await anext(chunks, b"")
                    return "ok", Routed(provider, reply, [], body=_starting_with(first, chunks))
                if reply.status_code == 200:
                    return "ok", Routed(provider, reply, [], payload=await reply.aread())
                outcome = self._refusal(provider, reply)
        except (TimeoutError, httpx.TimeoutException):
            outcome = "timeout"
        except httpx.HTTPError:
            outcome = "unreachable"
        except BaseException:
            await _close(reply)
            raise
        await _close(reply)
        return outcome, None

    def _refusal(self, provider: Provider, reply: httpx.Response) -> str:
        if reply.status_code == 429:
            pause = _seconds(reply.headers.get("retry-after"))
            if pause:
                self._cooling_until[provider.name] = self._clock() + pause
            return "rate_limited"
        if reply.status_code in CLIENT_ERRORS:
            return f"rejected_{reply.status_code}"
        return f"status_{reply.status_code}"
