"""Rewrites a chat completion stream so content and tool call arguments go through the redactor."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import AsyncIterator, Callable
from typing import Any

from gateway_common.sse import Event, SseParser
from llm_gateway.redact import StreamRedactor, redact

_ENVELOPE = ("id", "object", "created", "model", "system_fingerprint")


class UpstreamStreamError(Exception):
    """The provider sent an error in the middle of a stream."""


def _utf8_length(text: str) -> int:
    return len(text.encode())


class _Choice:
    def __init__(self) -> None:
        self.content = StreamRedactor()
        self.arguments: dict[int, StreamRedactor] = {}
        self.finished = False

    def redactors(self) -> list[StreamRedactor]:
        return [self.content, *self.arguments.values()]


class ChunkRewriter:
    """Keeps one redaction state per choice and per tool call argument stream."""

    def __init__(self, count_tokens: Callable[[str], int] = _utf8_length) -> None:
        self._choices: dict[int, _Choice] = {}
        self._count = count_tokens
        self._envelope: dict[str, Any] = {}
        self.completion_tokens = 0
        self.usage: dict[str, Any] | None = None

    @property
    def counts(self) -> Counter[str]:
        total: Counter[str] = Counter()
        for state in self._choices.values():
            for redactor in state.redactors():
                total += redactor.counts
        return total

    @property
    def largest_hold(self) -> int:
        holds = [r.largest_hold for state in self._choices.values() for r in state.redactors()]
        return max(holds, default=0)

    def rewrite(self, chunk: dict[str, Any]) -> list[dict[str, Any]]:
        """The chunks to send for one upstream chunk. Text released by a finish goes first."""
        self._envelope = {key: chunk[key] for key in _ENVELOPE if key in chunk}
        if isinstance(chunk.get("usage"), dict):
            self.usage = chunk["usage"]
        released: list[dict[str, Any]] = []
        for choice in chunk.get("choices") or []:
            if isinstance(choice, dict) and isinstance(choice.get("delta"), dict):
                released += self._rewrite_choice(choice)
        return [*released, chunk]

    def finish(self) -> list[dict[str, Any]]:
        """Chunks that carry what is still held for choices that never sent a finish reason."""
        released: list[dict[str, Any]] = []
        for index, state in self._choices.items():
            if not state.finished:
                released += self._release_rest(index, state)
        return released

    def _rewrite_choice(self, choice: dict[str, Any]) -> list[dict[str, Any]]:
        index = choice.get("index", 0)
        state = self._choices.setdefault(index, _Choice())
        delta = choice["delta"]
        finishing = choice.get("finish_reason") is not None
        released: list[dict[str, Any]] = []

        content = delta.get("content")
        if isinstance(content, str):
            self.completion_tokens += self._count(content)
            delta["content"] = state.content.feed(content)
            if finishing:
                delta["content"] += state.content.finish()

        fed: set[int] = set()
        for call in delta.get("tool_calls") or []:
            function = call.get("function") if isinstance(call, dict) else None
            if isinstance(function, dict) and isinstance(function.get("arguments"), str):
                call_index = call.get("index", 0)
                redactor = state.arguments.setdefault(call_index, StreamRedactor())
                self.completion_tokens += self._count(function["arguments"])
                function["arguments"] = redactor.feed(function["arguments"])
                if finishing:
                    function["arguments"] += redactor.finish()
                fed.add(call_index)

        if finishing:
            released += self._release_rest(index, state, isinstance(content, str), fed)
        return released

    def _release_rest(
        self, index: int, state: _Choice, content_done: bool = False, fed: set[int] | None = None
    ) -> list[dict[str, Any]]:
        released = []
        if not content_done and (rest := state.content.finish()):
            released.append(self._carrier(index, {"content": rest}))
        for call_index, redactor in state.arguments.items():
            if call_index not in (fed or set()) and (rest := redactor.finish()):
                call = {"index": call_index, "function": {"arguments": rest}}
                released.append(self._carrier(index, {"tool_calls": [call]}))
        state.finished = True
        return released

    def _carrier(self, index: int, delta: dict[str, Any]) -> dict[str, Any]:
        return {
            **self._envelope,
            "choices": [{"index": index, "delta": delta, "finish_reason": None}],
        }


def _encode(chunk: dict[str, Any], event: Event | None = None) -> bytes:
    data = json.dumps(chunk, ensure_ascii=False, separators=(",", ":"))
    return (event or Event()).with_data(data).encode()


def _rewrite_event(event: Event, rewriter: ChunkRewriter) -> list[bytes]:
    data = event.data
    if data is None:
        return [event.encode()]
    if data.strip() == "[DONE]":
        return [*(_encode(chunk) for chunk in rewriter.finish()), event.encode()]
    try:
        chunk = json.loads(data)
    except ValueError:
        chunk = None
    if not isinstance(chunk, dict):
        # Not a chunk this gateway understands. It still never leaves unredacted.
        return [event.with_data(redact(data)).encode()]
    if "error" in chunk:
        raise UpstreamStreamError
    rewritten = rewriter.rewrite(chunk)
    return [_encode(c) for c in rewritten[:-1]] + [_encode(rewritten[-1], event)]


async def rewrite_stream(
    upstream: AsyncIterator[bytes], rewriter: ChunkRewriter
) -> AsyncIterator[bytes]:
    """Parse the upstream SSE stream event by event and send each one on as soon as it is safe."""
    parser = SseParser()
    async for raw in upstream:
        for event in parser.feed(raw):
            for piece in _rewrite_event(event, rewriter):
                yield piece
    for event in parser.close():
        for piece in _rewrite_event(event, rewriter):
            yield piece
    for chunk in rewriter.finish():
        yield _encode(chunk)


def redact_completion(payload: dict[str, Any], counts: Counter[str]) -> dict[str, Any]:
    """Redact a whole, non streamed completion in place."""
    for choice in payload.get("choices") or []:
        message = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(message, dict):
            continue
        if isinstance(message.get("content"), str):
            message["content"] = redact(message["content"], counts)
        for call in message.get("tool_calls") or []:
            function = call.get("function") if isinstance(call, dict) else None
            if isinstance(function, dict) and isinstance(function.get("arguments"), str):
                function["arguments"] = redact(function["arguments"], counts)
    return payload
