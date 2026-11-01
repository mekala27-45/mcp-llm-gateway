"""Starts two mock providers with an LLM gateway in front of them, and reads streamed replies."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import httpx

from gateway_common.serving import background_server
from llm_gateway.app import create_app
from llm_gateway.settings import ProviderConfig, Settings
from mock_providers.app import MockProvider

SECRET = "llm-gateway-test-secret-0123456789abcdef"


@dataclass
class LlmStack:
    url: str
    primary: MockProvider
    backup: MockProvider
    settings: Settings


@contextmanager
def running_llm_stack(**overrides: Any) -> Iterator[LlmStack]:
    primary, backup = MockProvider("primary"), MockProvider("backup")
    with (
        background_server(primary.app()) as primary_url,
        background_server(backup.app()) as backup_url,
    ):
        settings = Settings(
            secret=SECRET,
            primary=ProviderConfig("primary", f"{primary_url}/v1"),
            backup=ProviderConfig("backup", f"{backup_url}/v1"),
            **overrides,
        )
        with background_server(create_app(settings)) as gateway_url:
            yield LlmStack(f"{gateway_url}/v1/chat/completions", primary, backup, settings)


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
