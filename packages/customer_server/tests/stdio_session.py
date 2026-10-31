"""Drives a customer server subprocess from a private event loop."""

import asyncio
import os
import sys
import threading
from collections.abc import Coroutine
from concurrent.futures import Future
from typing import Any, TypeVar

from mcp import Client, StdioServerParameters
from mcp.types import CallToolResult, ListToolsResult

T = TypeVar("T")


def server_env(**extra: str) -> dict[str, str]:
    """Environment for a server process. Coverage settings ride along to measure the child."""
    passthrough = {k: v for k, v in os.environ.items() if k.startswith("COVERAGE_")}
    return {**passthrough, "LOG_LEVEL": "WARNING", **extra}


class StdioSession:
    """One server process per test module, driven from a private event loop.

    The parametrized cases then cost one process start between them instead of one each.
    """

    def __init__(self, env: dict[str, str]) -> None:
        self._params = StdioServerParameters(
            command=sys.executable, args=["-m", "customer_server"], env=env
        )
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._ready = threading.Event()
        self._client: Client | None = None
        self._stop: asyncio.Event | None = None
        self._owner: Future[None] | None = None

    def start(self) -> None:
        self._thread.start()
        self._owner = asyncio.run_coroutine_threadsafe(self._hold_session(), self._loop)
        if not self._ready.wait(timeout=30):
            self._owner.result(timeout=1)
            raise RuntimeError("server did not start")

    async def _hold_session(self) -> None:
        self._stop = asyncio.Event()
        try:
            async with Client(self._params) as client:
                self._client = client
                self._ready.set()
                await self._stop.wait()
        finally:
            self._ready.set()

    def _run(self, coro: Coroutine[Any, Any, T]) -> T:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=20)

    def list_tools(self) -> ListToolsResult:
        assert self._client is not None
        return self._run(self._client.list_tools())

    def call(self, tool: str, arguments: dict[str, Any]) -> CallToolResult:
        assert self._client is not None
        return self._run(self._client.call_tool(tool, arguments))

    def close(self) -> None:
        if self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._owner is not None:
            self._owner.result(timeout=20)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        self._loop.close()
