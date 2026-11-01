"""Gateway settings, read from the environment."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    jwt_secret: str
    downstream_url: str = "http://127.0.0.1:8101/mcp"
    downstream_token: str | None = None
    jwt_issuer: str = "mcp-llm-gateway"
    jwt_audience: str = "mcp-gateway"
    max_body_bytes: int = 64 * 1024
    hide_admin_tools: bool = False
    listed_tools_only: bool = False

    def __post_init__(self) -> None:
        if len(self.jwt_secret.encode()) < 32:
            raise ValueError("the JWT secret must be at least 32 bytes for HS256")

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Settings:
        secret = env.get("GATEWAY_JWT_SECRET")
        if not secret:
            raise ValueError("GATEWAY_JWT_SECRET is required")
        return cls(
            jwt_secret=secret,
            downstream_url=env.get("GATEWAY_DOWNSTREAM_URL", cls.downstream_url),
            downstream_token=env.get("GATEWAY_DOWNSTREAM_TOKEN") or None,
            jwt_issuer=env.get("GATEWAY_JWT_ISSUER", cls.jwt_issuer),
            jwt_audience=env.get("GATEWAY_JWT_AUDIENCE", cls.jwt_audience),
            max_body_bytes=int(env.get("GATEWAY_MAX_BODY_BYTES", str(cls.max_body_bytes))),
            hide_admin_tools=_flag(env.get("GATEWAY_HIDE_ADMIN_TOOLS")),
            listed_tools_only=_flag(env.get("GATEWAY_LISTED_TOOLS_ONLY")),
        )
