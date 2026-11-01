"""One of the processes in the two process window test. It opens the shared SQLite file,
says ready, waits for a go line on stdin, sends concurrent admissions, and prints what it got
as JSON.

    python window_worker.py DB_PATH SEED
"""

from __future__ import annotations

import asyncio
import json
import random
import sys

from llm_gateway.db import Database
from llm_gateway.limiter import RateLimited, SlidingWindowLimiter

REQUESTS = 150


async def run(path: str, seed: int) -> dict[str, int]:
    db = await Database.open(path)
    limiter = SlidingWindowLimiter(db, limit=50_000)
    rng = random.Random(seed)
    counts = {"admitted": 0, "refused": 0}

    async def request(number: int) -> None:
        estimate = rng.randint(500, 3_000)
        # Spread over 50 ms, so the two processes' transactions mix.
        await asyncio.sleep(rng.random() / 20)
        try:
            admission = await limiter.admit("acme", f"p{seed}-{number}", estimate)
        except RateLimited:
            counts["refused"] += 1
            return
        counts["admitted"] += 1
        await limiter.reconcile(admission.request_id, rng.randint(estimate // 2, estimate))

    print("ready", flush=True)
    await asyncio.to_thread(sys.stdin.readline)
    try:
        await asyncio.gather(*(request(n) for n in range(REQUESTS)))
    finally:
        await db.close()
    return counts


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run(sys.argv[1], int(sys.argv[2])))))
