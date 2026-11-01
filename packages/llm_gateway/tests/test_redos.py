"""100,000 characters of input built to make each pattern backtrack. Every one must stay fast."""

import time

import pytest

from llm_gateway.redact import MAX_MATCH, StreamRedactor, redact

SIZE = 100_000

ADVERSARIAL = {
    "one long local part run": "a" * SIZE,
    "at signs with no domain": "a@" * (SIZE // 2),
    "endless labels": "a@" + "b." * (SIZE // 2 - 1),
    "local part of 64 then a bad domain": ("x" * 64 + "@" + "y." * 4 + "1 ") * (SIZE // 75),
    "one long digit run": "1" * SIZE,
    "digits split by spaces": "1 " * (SIZE // 2),
    "digits split by dashes": "1-" * (SIZE // 2),
    "ssn prefixes that never finish": "123-45-" * (SIZE // 7),
    "card candidates failing luhn": "4111 1111 1111 1112 " * (SIZE // 20),
}

BATCH_SECONDS = 2.0
STREAM_SECONDS = 5.0

# Coverage tracing slows Python code several times over, so these run without it.
pytestmark = pytest.mark.no_cover


@pytest.mark.parametrize("text", ADVERSARIAL.values(), ids=ADVERSARIAL.keys())
def test_batch_redaction_stays_linear(text: str) -> None:
    started = time.perf_counter()
    redact(text)
    assert time.perf_counter() - started < BATCH_SECONDS


@pytest.mark.parametrize("text", ADVERSARIAL.values(), ids=ADVERSARIAL.keys())
def test_streamed_redaction_stays_linear_with_a_bounded_hold(text: str) -> None:
    redactor = StreamRedactor()
    started = time.perf_counter()
    for start in range(0, len(text), 16):
        redactor.feed(text[start : start + 16])
    redactor.finish()
    assert time.perf_counter() - started < STREAM_SECONDS
    assert redactor.largest_hold <= MAX_MATCH
