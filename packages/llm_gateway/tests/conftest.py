from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from llm_gateway.limiter import SlidingWindowLimiter

Settle = Callable[..., Awaitable[None]]


@pytest.fixture
def settlements(monkeypatch: pytest.MonkeyPatch) -> Counter[str]:
    """How many times each request's reservation was reconciled or released."""
    calls: Counter[str] = Counter()

    def counted(settle: Settle) -> Settle:
        async def counting(self: SlidingWindowLimiter, request_id: str, *args: Any) -> None:
            calls[request_id] += 1
            await settle(self, request_id, *args)

        return counting

    for name in ("reconcile", "release"):
        original = getattr(SlidingWindowLimiter, name)
        monkeypatch.setattr(SlidingWindowLimiter, name, counted(original))
    return calls
