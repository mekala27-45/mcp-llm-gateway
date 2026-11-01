"""A token aware sliding window log per tenant, kept in SQLite.

The window lives on disk, so a restart keeps it and a second process sharing the file sees
it. Admission is one BEGIN IMMEDIATE transaction: evict, sum, decide and reserve with no
other writer in between.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from llm_gateway.db import Database

WINDOW_MS = 60_000


def wall_clock_ms() -> int:
    # Wall clock, not monotonic: two processes have to agree on what the time is.
    return time.time_ns() // 1_000_000


@dataclass(frozen=True)
class Admission:
    request_id: str
    tenant_id: str
    tokens: int
    at_ms: int


class RateLimited(Exception):
    def __init__(self, retry_after_s: int) -> None:
        super().__init__(f"retry after {retry_after_s}s")
        self.retry_after_s = retry_after_s


class EstimateTooLarge(Exception):
    """The request alone needs more than the whole window allows."""


class SlidingWindowLimiter:
    def __init__(
        self, db: Database, limit: int = 50_000, clock: Callable[[], int] = wall_clock_ms
    ) -> None:
        self._db = db
        self.limit = limit
        self._clock = clock

    async def admit(self, tenant_id: str, request_id: str, estimate: int) -> Admission:
        """Reserve estimate tokens for this request, or refuse it."""
        if estimate > self.limit:
            raise EstimateTooLarge
        retry_after: int | None = None
        async with self._db.transaction() as conn:
            now = self._clock()
            # An entry stops counting at exactly WINDOW_MS after it was admitted.
            await conn.execute(
                "DELETE FROM window_entries WHERE tenant_id = ? AND ts_ms <= ?",
                (tenant_id, now - WINDOW_MS),
            )
            cursor = await conn.execute(
                "SELECT ts_ms, tokens FROM window_entries WHERE tenant_id = ? ORDER BY ts_ms",
                (tenant_id,),
            )
            entries = [(int(ts), int(tokens)) for ts, tokens in await cursor.fetchall()]
            used = sum(tokens for _, tokens in entries)
            if used + estimate <= self.limit:
                await conn.execute(
                    "INSERT INTO window_entries (request_id, tenant_id, ts_ms, tokens, state) "
                    "VALUES (?, ?, ?, ?, 'reserved')",
                    (request_id, tenant_id, now, estimate),
                )
            else:
                retry_after = _retry_after(entries, used + estimate - self.limit, now)
        if retry_after is not None:
            raise RateLimited(retry_after)
        return Admission(request_id, tenant_id, estimate, now)

    async def reconcile(self, request_id: str, tokens: int) -> None:
        """Replace the reservation with what the request really used.

        The real number is written even when it is above the reservation, so the tenant
        pays for it in the next window instead of the limiter under counting.
        """
        await self._db.execute(
            "UPDATE window_entries SET tokens = ?, state = 'committed' WHERE request_id = ?",
            (tokens, request_id),
        )

    async def release(self, request_id: str) -> None:
        """Give back a reservation for a request that used no tokens."""
        await self._db.execute("DELETE FROM window_entries WHERE request_id = ?", (request_id,))


def _retry_after(entries: list[tuple[int, int]], needed: int, now: int) -> int:
    """Seconds until enough of the oldest entries have left the window to make room."""
    freed = 0
    for ts, tokens in entries:
        freed += tokens
        if freed >= needed:
            return max(1, math.ceil((ts + WINDOW_MS - now) / 1000))
    return math.ceil(WINDOW_MS / 1000)
