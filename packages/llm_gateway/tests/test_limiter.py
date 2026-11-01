from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from llm_gateway.db import Database
from llm_gateway.limiter import WINDOW_MS, EstimateTooLarge, RateLimited, SlidingWindowLimiter


class FakeClock:
    def __init__(self, now: int = 1_000_000) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = await Database.open(str(tmp_path / "limiter.db"))
    yield database
    await database.close()


async def test_admits_up_to_the_limit_and_refuses_past_it(db: Database) -> None:
    limiter = SlidingWindowLimiter(db, limit=50_000, clock=FakeClock())
    await limiter.admit("acme", "r1", 30_000)
    await limiter.admit("acme", "r2", 20_000)
    with pytest.raises(RateLimited):
        await limiter.admit("acme", "r3", 1)


async def test_tenants_have_separate_windows(db: Database) -> None:
    limiter = SlidingWindowLimiter(db, limit=50_000, clock=FakeClock())
    await limiter.admit("acme", "r1", 50_000)
    await limiter.admit("globex", "r2", 50_000)


async def test_an_estimate_over_the_limit_is_refused_on_its_own(db: Database) -> None:
    limiter = SlidingWindowLimiter(db, limit=50_000, clock=FakeClock())
    with pytest.raises(EstimateTooLarge):
        await limiter.admit("acme", "r1", 50_001)


async def test_retry_after_is_when_enough_tokens_leave_the_window(db: Database) -> None:
    clock = FakeClock()
    limiter = SlidingWindowLimiter(db, limit=50_000, clock=clock)
    await limiter.admit("acme", "r1", 20_000)
    clock.now += 10_000
    await limiter.admit("acme", "r2", 20_000)
    clock.now += 5_000
    with pytest.raises(RateLimited) as limited:
        await limiter.admit("acme", "r3", 25_000)
    # r1 leaves 60 s after it was admitted, which is 45 s from now, and that frees enough.
    assert limited.value.retry_after_s == 45


async def test_entries_stop_counting_at_exactly_sixty_seconds(db: Database) -> None:
    clock = FakeClock()
    limiter = SlidingWindowLimiter(db, limit=50_000, clock=clock)
    await limiter.admit("acme", "r1", 50_000)
    clock.now += WINDOW_MS - 1
    with pytest.raises(RateLimited) as limited:
        await limiter.admit("acme", "r2", 1)
    assert limited.value.retry_after_s == 1
    clock.now += 1
    await limiter.admit("acme", "r3", 50_000)


async def test_reconciling_an_overestimate_frees_tokens(db: Database) -> None:
    limiter = SlidingWindowLimiter(db, limit=50_000, clock=FakeClock())
    await limiter.admit("acme", "r1", 40_000)
    await limiter.reconcile("r1", 1_200)
    await limiter.admit("acme", "r2", 48_800)


async def test_reconcile_records_usage_above_the_reservation(db: Database) -> None:
    limiter = SlidingWindowLimiter(db, limit=50_000, clock=FakeClock())
    await limiter.admit("acme", "r1", 1_000)
    await limiter.reconcile("r1", 10_000)
    with pytest.raises(RateLimited):
        await limiter.admit("acme", "r2", 40_001)


async def test_releasing_a_failed_request_gives_the_tokens_back(db: Database) -> None:
    limiter = SlidingWindowLimiter(db, limit=50_000, clock=FakeClock())
    await limiter.admit("acme", "r1", 50_000)
    await limiter.release("r1")
    await limiter.admit("acme", "r2", 50_000)


async def test_the_window_survives_a_restart(tmp_path: Path) -> None:
    clock = FakeClock()
    first = await Database.open(str(tmp_path / "restart.db"))
    await SlidingWindowLimiter(first, limit=50_000, clock=clock).admit("acme", "r1", 45_000)
    await first.close()

    second = await Database.open(str(tmp_path / "restart.db"))
    try:
        with pytest.raises(RateLimited):
            await SlidingWindowLimiter(second, limit=50_000, clock=clock).admit("acme", "r2", 5_001)
    finally:
        await second.close()


async def test_entries_are_reserved_then_committed(db: Database) -> None:
    limiter = SlidingWindowLimiter(db, limit=50_000, clock=FakeClock())
    await limiter.admit("acme", "r1", 900)
    assert await db.fetch_all("SELECT tokens, state FROM window_entries") == [(900, "reserved")]
    await limiter.reconcile("r1", 321)
    assert await db.fetch_all("SELECT tokens, state FROM window_entries") == [(321, "committed")]
