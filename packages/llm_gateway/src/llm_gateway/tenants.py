"""Tenant API keys. Only an HMAC of each key is stored, never the key."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from llm_gateway.db import Database


class TenantStore:
    def __init__(self, db: Database, secret: str) -> None:
        self._db = db
        self._secret = secret.encode()

    def _digest(self, key: str) -> str:
        return hmac.new(self._secret, key.encode(), hashlib.sha256).hexdigest()

    async def add(self, tenant_id: str) -> str:
        """Create a tenant and return its key. The key is shown once and not kept."""
        key = "gk_" + secrets.token_urlsafe(32)
        await self._db.execute(
            "INSERT INTO tenants (tenant_id, key_hmac, created_ms) VALUES (?, ?, ?)",
            (tenant_id, self._digest(key), time.time_ns() // 1_000_000),
        )
        return key

    async def find(self, key: str) -> str | None:
        """The tenant a key belongs to, or None."""
        rows = await self._db.fetch_all(
            "SELECT tenant_id FROM tenants WHERE key_hmac = ?", (self._digest(key),)
        )
        return str(rows[0][0]) if rows else None


async def add_tenant(db_path: str, secret: str, tenant_id: str) -> str:
    """Open the database, add one tenant and return its key."""
    db = await Database.open(db_path)
    try:
        return await TenantStore(db, secret).add(tenant_id)
    finally:
        await db.close()
