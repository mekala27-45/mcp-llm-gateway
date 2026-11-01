"""Starts two mock providers with an LLM gateway in front of them, adds one tenant, and reads
streamed replies and the gateway's SQLite file."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import socket
import sqlite3
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.routing import Mount

from gateway_common.serving import background_server
from llm_gateway.app import create_app
from llm_gateway.limiter import wall_clock_ms
from llm_gateway.settings import ProviderConfig, Settings
from llm_gateway.tenants import add_tenant
from mock_providers.app import MockProvider

SECRET = "llm-gateway-test-secret-0123456789abcdef"


@dataclass
class LlmStack:
    url: str
    primary: MockProvider
    backup: MockProvider
    settings: Settings
    key: str

    @property
    def headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self.key}"}

    def rows(self, sql: str) -> list[tuple[Any, ...]]:
        """Rows from the gateway's SQLite file, read the way an operator would read them."""
        with closing(sqlite3.connect(self.settings.db_path)) as db:
            return db.execute(sql).fetchall()

    def logged(self, count: int = 1) -> list[tuple[Any, ...]]:
        """The request log once it has count rows: route, attempts, outcome, status, tokens.
        A stream is logged after its last byte, so this waits for it."""
        sql = "SELECT route, attempts, outcome, status, tokens FROM request_log ORDER BY rowid"
        eventually(lambda: len(self.rows(sql)) >= count)
        return self.rows(sql)


def eventually(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "the condition never held"
        time.sleep(0.02)


def refused_url() -> str:
    """A local address where nothing listens."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/v1"


@contextmanager
def running_llm_stack(
    clock: Callable[[], int] = wall_clock_ms,
    timeout_ms: int = 3000,
    primary_url: str | None = None,
    **overrides: Any,
) -> Iterator[LlmStack]:
    """primary_url points the gateway somewhere other than the primary mock."""
    primary, backup = MockProvider("primary"), MockProvider("backup")
    # Both mocks share one server: a test starts two servers instead of three.
    mocks = Starlette(routes=[Mount("/primary", primary.app()), Mount("/backup", backup.app())])
    with tempfile.TemporaryDirectory() as scratch, background_server(mocks) as mock_url:
        primary_config = ProviderConfig(
            "primary",
            primary_url or f"{mock_url}/primary/v1",
            api_key="mock-primary-key",
            timeout_ms=timeout_ms,
        )
        backup_config = ProviderConfig(
            "backup", f"{mock_url}/backup/v1", api_key="mock-backup-key", timeout_ms=timeout_ms
        )
        settings = Settings(
            secret=SECRET,
            primary=primary_config,
            backup=backup_config,
            db_path=f"{scratch}/gateway.db",
        )
        settings = dataclasses.replace(settings, **overrides)
        key = asyncio.run(add_tenant(settings.db_path, SECRET, "acme"))
        with background_server(create_app(settings, clock=clock)) as gateway_url:
            url = f"{gateway_url}/v1/chat/completions"
            yield LlmStack(url, primary, backup, settings, key)


def chat(stream: bool = True, **extra: Any) -> dict[str, Any]:
    return {
        "model": "mock-model",
        "messages": [{"role": "user", "content": "Where is my refund?"}],
        "stream": stream,
        **extra,
    }


def chunks_of(lines: Iterator[str]) -> list[dict[str, Any]]:
    """The JSON chunks of an SSE reply, without the [DONE] marker."""
    data = [line[len("data:") :].strip() for line in lines if line.startswith("data:")]
    return [json.loads(item) for item in data if item != "[DONE]"]


def text_of(chunks: list[dict[str, Any]], index: int = 0) -> str:
    return "".join(
        choice["delta"].get("content") or ""
        for chunk in chunks
        for choice in chunk.get("choices", [])
        if choice.get("index") == index
    )


def post_stream(url: str, body: dict[str, Any], headers: dict[str, str]) -> list[dict[str, Any]]:
    with httpx.stream("POST", url, json=body, headers=headers, timeout=30) as response:
        assert response.status_code == 200, response.read()
        return chunks_of(response.iter_lines())
