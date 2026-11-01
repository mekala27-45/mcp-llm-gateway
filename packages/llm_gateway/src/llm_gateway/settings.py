"""LLM gateway settings, read from the environment and an optional local .env file."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProviderConfig:
    """One OpenAI compatible provider. The name is for logs only and never reaches a client."""

    name: str
    base_url: str
    api_key: str | None = None
    model: str | None = None
    timeout_ms: int = 3000


@dataclass(frozen=True)
class Settings:
    secret: str
    primary: ProviderConfig
    backup: ProviderConfig | None = None
    db_path: str = "data/llm_gateway.db"
    tokens_per_minute: int = 50_000
    default_max_tokens: int = 1024
    max_body_bytes: int = 1 << 20

    def __post_init__(self) -> None:
        if len(self.secret.encode()) < 32:
            raise ValueError("the gateway secret must be at least 32 bytes")

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Settings:
        secret = env.get("LLM_GATEWAY_SECRET")
        if not secret:
            raise ValueError("LLM_GATEWAY_SECRET is required")

        def provider(prefix: str, default_url: str | None) -> ProviderConfig | None:
            url = env.get(f"{prefix}_BASE_URL", default_url)
            if not url:
                return None
            return ProviderConfig(
                name=prefix.lower(),
                base_url=url,
                api_key=env.get(f"{prefix}_API_KEY") or None,
                model=env.get(f"{prefix}_MODEL") or None,
                timeout_ms=int(env.get(f"{prefix}_TIMEOUT_MS", "3000")),
            )

        primary = provider("PRIMARY", "http://127.0.0.1:8201/v1")
        assert primary is not None
        return cls(
            secret=secret,
            primary=primary,
            backup=provider("BACKUP", None),
            db_path=env.get("LLM_GATEWAY_DB", cls.db_path),
            tokens_per_minute=int(env.get("TOKENS_PER_MINUTE", str(cls.tokens_per_minute))),
        )


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines from a local .env file. Blank lines and # comments are skipped."""
    if not path.is_file():
        return {}
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("'\"")
    return values
