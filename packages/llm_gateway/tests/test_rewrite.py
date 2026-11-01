import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from llm_gateway.redact import REDACTED
from llm_gateway.stream import ChunkRewriter, UpstreamStreamError, rewrite_stream


def chunk(index: int = 0, finish: str | None = None, **delta: Any) -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "m",
        "system_fingerprint": "fp",
        "choices": [{"index": index, "delta": delta, "finish_reason": finish, "logprobs": None}],
    }


def args(text: str, call: int = 0) -> dict[str, Any]:
    return {"tool_calls": [{"index": call, "function": {"arguments": text}}]}


def sse(*chunks: dict[str, Any] | str) -> list[bytes]:
    return [
        f"data: {item if isinstance(item, str) else json.dumps(item)}\n\n".encode()
        for item in chunks
    ]


async def run(pieces: list[bytes]) -> list[dict[str, Any] | str]:
    async def upstream() -> AsyncIterator[bytes]:
        for piece in pieces:
            yield piece

    out = b"".join([piece async for piece in rewrite_stream(upstream(), ChunkRewriter())])
    items = [line[6:] for line in out.decode().splitlines() if line.startswith("data: ")]
    return [json.loads(item) if item.startswith("{") else item for item in items]


def text(items: list[dict[str, Any] | str], index: int = 0) -> str:
    return "".join(
        choice["delta"].get("content") or ""
        for item in items
        if isinstance(item, dict)
        for choice in item["choices"]
        if choice["index"] == index
    )


async def test_each_choice_has_its_own_redaction_state() -> None:
    items = await run(
        sse(
            chunk(0, content="card 4111 1111"),
            chunk(1, content="email ada@exa"),
            chunk(0, content=" 1111 1111 ok"),
            chunk(1, content="mple.com ok"),
            chunk(0, finish="stop"),
            chunk(1, finish="stop"),
            "[DONE]",
        )
    )
    assert text(items, 0) == f"card {REDACTED} ok"
    assert text(items, 1) == f"email {REDACTED} ok"
    assert items[-1] == "[DONE]"


async def test_tool_call_arguments_are_redacted_per_call() -> None:
    items = await run(
        sse(
            chunk(**args('{"ssn": "123-4')),
            chunk(**args('5-6789", "note": "x"}')),
            chunk(finish="tool_calls"),
            "[DONE]",
        )
    )
    arguments = "".join(
        call["function"]["arguments"]
        for item in items
        if isinstance(item, dict)
        for choice in item["choices"]
        for call in choice["delta"].get("tool_calls", [])
    )
    assert json.loads(arguments) == {"ssn": REDACTED, "note": "x"}


async def test_held_text_comes_out_before_the_finish_chunk() -> None:
    items = await run(
        sse(chunk(content="write to john@example.com"), chunk(finish="stop"), "[DONE]")
    )
    assert text(items) == f"write to {REDACTED}"
    finish_at = next(
        i
        for i, item in enumerate(items)
        if isinstance(item, dict) and item["choices"][0]["finish_reason"]
    )
    assert text(items[:finish_at]) == f"write to {REDACTED}"


async def test_held_text_is_released_at_done_when_no_finish_reason_came() -> None:
    items = await run(sse(chunk(content="last word"), "[DONE]"))
    assert text(items) == "last word"


async def test_every_other_field_of_a_chunk_is_kept() -> None:
    original = chunk(content="hello there ")
    [rewritten, *_] = await run(sse(original, "[DONE]"))
    assert isinstance(rewritten, dict)
    assert {k: v for k, v in rewritten.items() if k != "choices"} == {
        k: v for k, v in original.items() if k != "choices"
    }
    assert rewritten["choices"][0]["logprobs"] is None


async def test_data_that_is_not_a_chunk_still_goes_through_the_redactor() -> None:
    items = await run(sse("plain text with 123-45-6789 in it"))
    assert items == [f"plain text with {REDACTED} in it"]


async def test_an_error_inside_the_stream_stops_it() -> None:
    with pytest.raises(UpstreamStreamError):
        await run(sse(chunk(content="hi "), {"error": {"message": "node-7 crashed"}}))
