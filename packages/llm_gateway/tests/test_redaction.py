import logging

import httpx
import pytest
from llm_stack import chat, post_stream, running_llm_stack, text_of

from llm_gateway.redact import REDACTED
from mock_providers.app import DEFAULT_TEXT, Behavior, tokens

RAW_PII = ("ada.lindqvist@example.com", "4111 1111 1111 1111", "123-45-6789")


def test_pii_in_a_stream_arrives_redacted(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="llm_gateway")
    with running_llm_stack() as stack:
        chunks = post_stream(stack.url, chat(), {})
    streamed = text_of(chunks)
    assert streamed.count(REDACTED) == 3
    for value in RAW_PII:
        assert value not in streamed
    completion = next(r for r in caplog.records if r.getMessage() == "completion")
    assert completion.__dict__["fields"]["redactions"] == {"email": 1, "card": 1, "ssn": 1}
    for value in RAW_PII:
        assert value not in caplog.text


def test_a_card_split_over_four_chunks_arrives_redacted() -> None:
    pieces = ["Charged ", "4111 11", "11 111", "1 11", "11 today."]
    with running_llm_stack() as stack:
        stack.primary.behavior = Behavior(choices=[pieces])
        chunks = post_stream(stack.url, chat(), {})
    assert text_of(chunks) == f"Charged {REDACTED} today."


def test_two_choices_and_a_tool_call_are_redacted_separately() -> None:
    with running_llm_stack() as stack:
        stack.primary.behavior = Behavior(
            choices=[tokens("ssn 123-45-6789 ok"), tokens("mail ada@example.com ok")],
            tool_arguments=['{"card": "4111 1111 ', '1111 1111"}'],
        )
        chunks = post_stream(stack.url, chat(n=2), {})
    arguments = "".join(
        call["function"]["arguments"]
        for chunk in chunks
        for choice in chunk["choices"]
        for call in choice["delta"].get("tool_calls", [])
    )
    assert text_of(chunks, 0) == f"ssn {REDACTED} ok"
    assert text_of(chunks, 1) == f"mail {REDACTED} ok"
    assert arguments == f'{{"card": "{REDACTED}"}}'


def test_a_non_streamed_completion_is_redacted_too() -> None:
    with running_llm_stack() as stack:
        reply = httpx.post(stack.url, json=chat(stream=False))
    content = reply.json()["choices"][0]["message"]["content"]
    assert content.count(REDACTED) == 3
    assert content != DEFAULT_TEXT
