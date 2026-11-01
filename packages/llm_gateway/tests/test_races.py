"""The races: a primary answering at 2,950 ms and at 3,050 ms against its 3,000 ms timeout, a
primary 429, a refused connection, and both providers failing. Each ends in one answer from
one provider, one settlement of the reservation, and nothing late from the primary used.

Every race runs a streamed and a whole request side by side. The timeout covers different
things for the two (the first byte, or the whole reply), and running them together keeps the
three second cases to one wait each."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import httpx
import pytest
from llm_stack import LlmStack, chat, chunks_of, eventually, refused_url, running_llm_stack, text_of

from mock_providers.app import Behavior

# The margins here are 50 ms, so these tests run without coverage tracing slowing them.
pytestmark = pytest.mark.no_cover

PRIMARY = "The primary answered this one."
BACKUP = "The backup answered this one."
IDS = {PRIMARY: {"chatcmpl-primary"}, BACKUP: {"chatcmpl-backup"}, "": set()}


@dataclass
class Race:
    primary: Behavior
    backup: Behavior
    status: int
    answer: str
    attempts: str
    primary_end: str
    refused: bool = False


RACES = {
    "primary_at_2950_ms": Race(
        Behavior(text=PRIMARY, first_byte_delay=2.95),
        Behavior(text=BACKUP),
        200,
        PRIMARY,
        "primary:ok",
        primary_end="answered",
    ),
    "primary_at_3050_ms": Race(
        Behavior(text=PRIMARY, first_byte_delay=3.05),
        Behavior(text=BACKUP),
        200,
        BACKUP,
        "primary:timeout,backup:ok",
        primary_end="cancelled",
    ),
    # No Retry-After, so neither request sees the primary cooling down after the other.
    "primary_429": Race(
        Behavior(status=429),
        Behavior(text=BACKUP),
        200,
        BACKUP,
        "primary:rate_limited,backup:ok",
        primary_end="refused",
    ),
    "primary_connection_refused": Race(
        Behavior(),
        Behavior(text=BACKUP),
        200,
        BACKUP,
        "primary:unreachable,backup:ok",
        primary_end="never_reached",
        refused=True,
    ),
    "both_failing": Race(
        Behavior(status=500),
        Behavior(status=429),
        503,
        "",
        "primary:status_500,backup:rate_limited",
        primary_end="refused",
    ),
}


def send(stack: LlmStack, stream: bool) -> tuple[int, str, set[str]]:
    """The status, the answer, and the ids on every chunk or body that reached the client."""
    reply = httpx.post(stack.url, json=chat(stream=stream), headers=stack.headers, timeout=30)
    if reply.status_code != 200:
        return reply.status_code, "", set()
    if stream:
        chunks = chunks_of(iter(reply.text.splitlines()))
        return 200, text_of(chunks), {chunk["id"] for chunk in chunks}
    payload = reply.json()
    return 200, payload["choices"][0]["message"]["content"], {payload["id"]}


def primary_ends(stack: LlmStack) -> set[str]:
    """How the primary mock saw its requests end."""
    if not stack.primary.requests:
        return {"never_reached"}
    return {
        "answered" if seen.finished else "cancelled" if seen.disconnected else "refused"
        for seen in stack.primary.requests
    }


@pytest.mark.parametrize("name", RACES)
def test_each_race_ends_in_one_answer_and_one_settlement(
    name: str, settlements: Counter[str]
) -> None:
    race = RACES[name]
    with running_llm_stack(primary_url=refused_url() if race.refused else None) as stack:
        stack.primary.behavior, stack.backup.behavior = race.primary, race.backup
        with ThreadPoolExecutor(2) as pool:
            replies = list(pool.map(lambda stream: send(stack, stream), [True, False]))
        log = stack.logged(2)
        eventually(lambda: primary_ends(stack) == {race.primary_end})
    for status, answer, ids in replies:
        assert (status, answer) == (race.status, race.answer)
        assert ids == IDS[race.answer]
    assert [row[1] for row in log] == [race.attempts] * 2
    assert sorted(settlements.values()) == [1, 1]
