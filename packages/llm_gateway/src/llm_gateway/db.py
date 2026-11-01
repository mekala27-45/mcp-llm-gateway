"""The SQLite file that holds tenants, the rate limit window and the request log."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id TEXT PRIMARY KEY,
    key_hmac TEXT NOT NULL UNIQUE,
    created_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS window_entries (
    request_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    tokens INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('reserved', 'committed'))
);
CREATE INDEX IF NOT EXISTS window_by_tenant ON window_entries (tenant_id, ts_ms);
CREATE TABLE IF NOT EXISTS request_log (
    request_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    created_ms INTEGER NOT NULL,
    route TEXT NOT NULL,
    attempts TEXT NOT NULL,
    outcome TEXT NOT NULL,
    status INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL,
    tokens INTEGER
);
"""


class Database:
    """One connection per process. Every statement takes the lock, so a statement from one
    request can never land inside another request's open transaction."""

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection
        self._lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: str) -> Database:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None leaves transactions to BEGIN and COMMIT written out below.
        connection = await aiosqlite.connect(path, isolation_level=None)
        await connection.execute("PRAGMA journal_mode=WAL")
        await connection.execute("PRAGMA busy_timeout=5000")
        await connection.execute("PRAGMA synchronous=NORMAL")
        await connection.executescript(SCHEMA)
        return cls(connection)

    async def close(self) -> None:
        await self._connection.close()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """BEGIN IMMEDIATE takes the write lock up front, so two processes cannot both read
        the same window and both decide there is room."""
        async with self._lock:
            await self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                await self._connection.execute("ROLLBACK")
                raise
            await self._connection.execute("COMMIT")

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Run one statement on its own and return the number of rows it changed."""
        async with self._lock:
            cursor = await self._connection.execute(sql, tuple(params))
            return cursor.rowcount

    async def fetch_all(self, sql: str, params: Iterable[Any] = ()) -> list[tuple[Any, ...]]:
        async with self._lock:
            cursor = await self._connection.execute(sql, tuple(params))
            return [tuple(row) for row in await cursor.fetchall()]
