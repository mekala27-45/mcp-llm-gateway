"""Token estimates for the limiter. They are estimates: other model families count differently,
and the reservation is reconciled with the real usage once the reply is done."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

# Framing the chat format adds per message, and the tokens that prime the reply.
MESSAGE_OVERHEAD = 4
REPLY_PRIMING = 3


def _utf8_length(text: str) -> int:
    return len(text.encode())


class TokenEstimator:
    """UTF-8 length by default. A byte level BPE tokenizer never makes more tokens than bytes,
    so the default can only overestimate, and it works offline.

    LLM_GATEWAY_TIKTOKEN=1 switches to tiktoken's o200k_base. tiktoken downloads its BPE file
    the first time and has no timeout on that download, which is why it is not the default.
    """

    def __init__(self, use_tiktoken: bool | None = None) -> None:
        if use_tiktoken is None:
            use_tiktoken = os.environ.get("LLM_GATEWAY_TIKTOKEN") == "1"
        self.count: Callable[[str], int] = _utf8_length
        self.method = "utf-8 length"
        if use_tiktoken:
            import tiktoken

            encoding = tiktoken.get_encoding("o200k_base")
            self.count = lambda text: len(encoding.encode(text, disallowed_special=()))
            self.method = "tiktoken o200k_base"

    def prompt(self, messages: list[Any]) -> int:
        total = REPLY_PRIMING
        for message in messages:
            total += MESSAGE_OVERHEAD
            if isinstance(message, dict):
                total += self.count(_text(message.get("content")))
                total += self.count(_text(message.get("name")))
                total += self.count(_text(message.get("tool_calls")))
        return total


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(
            _text(part.get("text") if isinstance(part, dict) else part) for part in value
        )
    return str(value)
