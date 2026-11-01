"""Runs an ASGI app on a local port in a background thread, for the demo, tests and bench."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import uvicorn
from starlette.types import ASGIApp


class _Server(uvicorn.Server):
    """uvicorn looks for a stop request every 100 ms. The tests start and stop a few hundred
    servers, so this one looks every 10 ms."""

    async def main_loop(self) -> None:
        counter = 0
        while not await self.on_tick(counter):
            counter = (counter + 1) % 864_000
            await asyncio.sleep(0.01)


@contextmanager
def background_server(app: ASGIApp, port: int = 0) -> Iterator[str]:
    """Serve app on 127.0.0.1 until the block exits. Yields the base URL."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # Binding before uvicorn starts means port 0 picks a free port with no race.
    sock.bind(("127.0.0.1", port))
    config = uvicorn.Config(
        app, log_config=None, access_log=False, lifespan="on", timeout_graceful_shutdown=2
    )
    server = _Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            sock.close()
            raise RuntimeError("background server did not start")
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
