"""OpenAI compatible chat completion providers whose behavior each test sets."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

DEFAULT_TEXT = (
    "Your refund is on its way. We sent the receipt to ada.lindqvist@example.com and charged "
    "the card 4111 1111 1111 1111. The SSN on file ends the same as 123-45-6789."
)

# What a careless provider puts in an error page: a stack trace, a host and a path.
LEAKY_ERROR = (
    "Traceback (most recent call last):\n"
    '  File "/srv/inference/router/dispatch.py", line 88, in route\n'
    "ConnectionError: inference-node-07.internal.example.net:9443 refused the connection\n"
)

_TOKEN = re.compile(r"\s*\S{1,4}|\s+")


def tokens(text: str) -> list[str]:
    """Cut text into pieces of about four characters, the way a model streams it."""
    return _TOKEN.findall(text)


@dataclass
class Behavior:
    """What the next requests to a mock provider get."""

    status: int = 200
    first_byte_delay: float = 0.0
    token_interval: float = 0.0
    text: str = DEFAULT_TEXT
    choices: list[list[str]] | None = None
    tool_arguments: list[str] | None = None
    repeat: int = 1
    chunk_chars: int | None = None
    retry_after: int | None = None
    error_body: str = LEAKY_ERROR
    report_usage: bool = True


@dataclass
class Seen:
    """One request as the mock received it."""

    model: str
    stream: bool
    max_tokens: int | None
    authorization: str | None
    received_at: float = field(default_factory=time.monotonic)
    finished: bool = False
    disconnected: bool = False


def _prompt_words(body: dict[str, Any]) -> int:
    messages = body.get("messages", [])
    return sum(len(str(m.get("content", "")).split()) for m in messages if isinstance(m, dict))


class MockProvider:
    """One mock provider. Change .behavior between requests; read .requests afterwards."""

    def __init__(self, name: str, behavior: Behavior | None = None) -> None:
        self.name = name
        self.behavior = behavior or Behavior()
        self.requests: list[Seen] = []

    def app(self) -> Starlette:
        return Starlette(routes=[Route("/v1/chat/completions", self._complete, methods=["POST"])])

    async def _complete(self, request: Request) -> Response:
        body = await request.json()
        behavior = self.behavior
        seen = Seen(
            model=str(body.get("model")),
            stream=bool(body.get("stream")),
            max_tokens=body.get("max_tokens"),
            authorization=request.headers.get("authorization"),
        )
        self.requests.append(seen)
        if behavior.first_byte_delay:
            await asyncio.sleep(behavior.first_byte_delay)
        if behavior.status == 429:
            headers = {"retry-after": str(behavior.retry_after)} if behavior.retry_after else {}
            error = {"error": {"message": "rate limit reached", "type": "rate_limit_error"}}
            return JSONResponse(error, status_code=429, headers=headers)
        if behavior.status >= 400:
            return PlainTextResponse(behavior.error_body, status_code=behavior.status)

        pieces = self._pieces(behavior, body.get("max_tokens"))
        if not seen.stream:
            seen.finished = True
            return JSONResponse(self._completion(body, pieces))
        return StreamingResponse(self._events(body, pieces, seen), media_type="text/event-stream")

    def _pieces(self, behavior: Behavior, max_tokens: int | None) -> list[list[str]]:
        size = behavior.chunk_chars
        text = behavior.text
        base = [text[i : i + size] for i in range(0, len(text), size)] if size else tokens(text)
        per_choice = behavior.choices or [base * behavior.repeat]
        if isinstance(max_tokens, int):
            per_choice = [pieces[:max_tokens] for pieces in per_choice]
        return per_choice

    def _usage(self, body: dict[str, Any], pieces: list[list[str]]) -> dict[str, int]:
        prompt = _prompt_words(body)
        completion = sum(len(p) for p in pieces) + len(self.behavior.tool_arguments or [])
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        }

    def _envelope(self, body: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": f"chatcmpl-{self.name}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": str(body.get("model")),
            "system_fingerprint": f"fp_{self.name}",
        }

    def _completion(self, body: dict[str, Any], pieces: list[list[str]]) -> dict[str, Any]:
        choices = [
            {
                "index": index,
                "message": {"role": "assistant", "content": "".join(texts)},
                "finish_reason": "stop",
            }
            for index, texts in enumerate(pieces)
        ]
        reply = {**self._envelope(body), "object": "chat.completion", "choices": choices}
        if self.behavior.report_usage:
            reply["usage"] = self._usage(body, pieces)
        return reply

    def _event(self, body: dict[str, Any], choices: list[dict[str, Any]], **extra: Any) -> bytes:
        chunk = {**self._envelope(body), "choices": choices, **extra}
        return f"data: {json.dumps(chunk)}\n\n".encode()

    async def _events(
        self, body: dict[str, Any], pieces: list[list[str]], seen: Seen
    ) -> AsyncIterator[bytes]:
        opening = {"role": "assistant", "content": ""}
        try:
            yield self._event(body, [_choice(i, opening) for i in range(len(pieces))])
            for step in range(max((len(p) for p in pieces), default=0)):
                for index, texts in enumerate(pieces):
                    if step < len(texts):
                        if self.behavior.token_interval:
                            await asyncio.sleep(self.behavior.token_interval)
                        yield self._event(body, [_choice(index, {"content": texts[step]})])
            finish = "stop"
            if self.behavior.tool_arguments:
                finish = "tool_calls"
                yield self._event(body, [_choice(0, {"tool_calls": [_call("", name="lookup")]})])
                for piece in self.behavior.tool_arguments:
                    yield self._event(body, [_choice(0, {"tool_calls": [_call(piece)]})])
            yield self._event(body, [_choice(i, {}, finish) for i in range(len(pieces))])
            if self.behavior.report_usage and (body.get("stream_options") or {}).get(
                "include_usage"
            ):
                yield self._event(body, [], usage=self._usage(body, pieces))
            yield b"data: [DONE]\n\n"
            seen.finished = True
        finally:
            seen.disconnected = not seen.finished


def _choice(index: int, delta: dict[str, Any], finish_reason: str | None = None) -> dict[str, Any]:
    return {"index": index, "delta": delta, "finish_reason": finish_reason}


def _call(arguments: str, name: str | None = None) -> dict[str, Any]:
    if name is None:
        return {"index": 0, "function": {"arguments": arguments}}
    return {
        "index": 0,
        "id": "call_0",
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }
