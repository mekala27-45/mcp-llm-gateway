"""No 60 second window ever holds more than the limit for a tenant. Checked by replaying the
committed entries under concurrency with an injected clock, across two processes on one
SQLite file, and by watching the window while the gateway serves 200 requests at once."""

import asyncio
import json
import random
import sqlite3
import subprocess
import sys
from collections import Counter
from contextlib import closing
from pathlib import Path

import httpx
from llm_stack import LlmStack, chat, running_llm_stack

from llm_gateway.db import Database
from llm_gateway.limiter import WINDOW_MS, RateLimited, SlidingWindowLimiter
from mock_providers.app import Behavior

LIMIT = 50_000
WORKER = Path(__file__).with_name("window_worker.py")


def busiest_window(entries: list[tuple[int, int]]) -> int:
    """The most tokens any 60 second window holds, from (admitted at, tokens) entries."""
    return max(
        sum(tokens for at, tokens in entries if end - WINDOW_MS < at <= end) for end, _ in entries
    )


class TickingClock:
    """Moves forward a random step each time it is read, so the window slides during a test."""

    def __init__(self, rng: random.Random) -> None:
        self._rng = rng
        self.now = 1_000_000

    def __call__(self) -> int:
        self.now += self._rng.randint(0, 1_500)
        return self.now


async def test_no_window_holds_more_than_the_limit_under_concurrency(tmp_path: Path) -> None:
    rng = random.Random(20261031)
    db = await Database.open(str(tmp_path / "window.db"))
    limiter = SlidingWindowLimiter(db, LIMIT, TickingClock(rng))
    committed: list[tuple[int, int]] = []
    refused = 0

    async def request(number: int) -> None:
        nonlocal refused
        estimate = rng.randint(500, 9_000)
        await asyncio.sleep(rng.random() / 50)
        try:
            admission = await limiter.admit("acme", f"r{number}", estimate)
        except RateLimited:
            refused += 1
            return
        await asyncio.sleep(rng.random() / 100)
        if rng.random() < 0.1:
            # The provider failed before any tokens were used.
            await limiter.release(admission.request_id)
            return
        used = rng.randint(estimate // 2, estimate)
        await limiter.reconcile(admission.request_id, used)
        committed.append((admission.at_ms, used))

    try:
        await asyncio.gather(*(request(number) for number in range(200)))
    finally:
        await db.close()
    assert busiest_window(committed) <= LIMIT
    # The limit was hit over and over, and the window slid more than once.
    assert refused > 50
    assert max(committed)[0] - min(committed)[0] > 2 * WINDOW_MS


def test_two_processes_on_one_file_share_one_window(tmp_path: Path) -> None:
    path = tmp_path / "shared.db"
    workers = [
        subprocess.Popen(
            [sys.executable, str(WORKER), str(path), str(seed)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        for seed in (1, 2)
    ]
    # Both open the new file first, then start together.
    for worker in workers:
        assert worker.stdout is not None
        assert worker.stdout.readline().strip() == "ready"
    for worker in workers:
        assert worker.stdin is not None
        worker.stdin.write("go\n")
        worker.stdin.flush()
    reports = [json.loads(worker.communicate(timeout=60)[0]) for worker in workers]
    with closing(sqlite3.connect(path)) as db:
        entries = db.execute("SELECT ts_ms, tokens, state FROM window_entries").fetchall()
    assert len(entries) == sum(report["admitted"] for report in reports)
    assert {state for _, _, state in entries} == {"committed"}
    assert busiest_window([(at, tokens) for at, tokens, _ in entries]) <= LIMIT
    assert sum(report["refused"] for report in reports) > 0


async def flood(stack: LlmStack, count: int) -> tuple[list[httpx.Response], list[int]]:
    """Send count streaming requests at once, sampling the tokens the window holds meanwhile."""
    rng = random.Random(7)
    held: list[int] = []

    async def watch() -> None:
        while True:
            held.append(stack.rows("SELECT total(tokens) FROM window_entries")[0][0])
            await asyncio.sleep(0.005)

    async def request(client: httpx.AsyncClient) -> httpx.Response:
        body = chat(max_tokens=rng.randint(2_000, 8_000))
        return await client.post(stack.url, json=body, headers=stack.headers)

    limits = httpx.Limits(max_connections=count)
    async with httpx.AsyncClient(timeout=60, limits=limits) as client:
        watcher = asyncio.create_task(watch())
        replies = await asyncio.gather(*(request(client) for _ in range(count)))
        watcher.cancel()
    return replies, held


def test_the_gateway_never_holds_more_than_the_limit_under_200_requests(
    settlements: Counter[str],
) -> None:
    with running_llm_stack() as stack:
        stack.primary.behavior = Behavior(text="Your refund is on its way.", token_interval=0.01)
        replies, held = asyncio.run(flood(stack, 200))
        stack.logged(200)
        reserved = stack.rows("SELECT count(*) FROM window_entries WHERE state = 'reserved'")

    statuses = Counter(reply.status_code for reply in replies)
    assert set(statuses) == {200, 429}
    assert all(reply.headers["retry-after"] for reply in replies if reply.status_code == 429)
    assert max(held) <= LIMIT
    # Every admitted request was settled once, and nothing is left reserved.
    assert sorted(settlements.values()) == [1] * statuses[200]
    assert reserved == [(0,)]
