import json
import logging
from typing import Any

import httpx
import pytest
from llm_stack import (
    chat,
    chunks_of,
    eventually,
    post_stream,
    refused_url,
    running_llm_stack,
    text_of,
)

from llm_gateway.providers import Provider
from mock_providers.app import LEAKY_ERROR, Behavior

PLAIN = "Your refund was approved and should arrive within three days."

# What must never reach a client: the mock's stack trace, host and path, the provider names,
# where the providers live, and the library underneath.
LEAKS = (
    "Traceback",
    "dispatch.py",
    "/srv/",
    "inference-node",
    "internal.example.net",
    "9443",
    "primary",
    "backup",
    "127.0.0.1",
    "httpx",
    "RuntimeError",
)


def answer(reply: httpx.Response, stream: bool) -> str:
    if stream:
        return text_of(chunks_of(iter(reply.text.splitlines())))
    content: str = reply.json()["choices"][0]["message"]["content"]
    return content


def test_a_429_goes_to_the_backup_and_cools_the_primary_down() -> None:
    with running_llm_stack() as stack:
        stack.primary.behavior = Behavior(status=429, retry_after=30)
        stack.backup.behavior = Behavior(text=PLAIN)
        first = post_stream(stack.url, chat(), stack.headers)
        second = post_stream(stack.url, chat(), stack.headers)
        log = stack.logged(2)
        primary_calls = len(stack.primary.requests)
    assert text_of(first) == text_of(second) == PLAIN
    assert {chunk["id"] for chunk in first + second} == {"chatcmpl-backup"}
    assert primary_calls == 1
    assert [row[1] for row in log] == ["primary:rate_limited,backup:ok", "backup:ok"]


@pytest.mark.parametrize("stream", [True, False], ids=["stream", "whole"])
def test_a_primary_past_its_timeout_is_cancelled_for_the_backup(stream: bool) -> None:
    with running_llm_stack(timeout_ms=300) as stack:
        stack.primary.behavior = Behavior(text="late answer", first_byte_delay=0.6)
        stack.backup.behavior = Behavior(text=PLAIN)
        reply = httpx.post(stack.url, json=chat(stream=stream), headers=stack.headers)
        log = stack.logged()
        late = stack.primary.requests[-1]
        eventually(lambda: late.disconnected)
    assert reply.status_code == 200
    assert answer(reply, stream) == PLAIN
    assert log[0][:3] == ("backup", "primary:timeout,backup:ok", "ok")
    assert not late.finished


def test_a_refused_connection_goes_to_the_backup() -> None:
    with running_llm_stack(primary_url=refused_url()) as stack:
        stack.backup.behavior = Behavior(text=PLAIN)
        chunks = post_stream(stack.url, chat(), stack.headers)
        log = stack.logged()
    assert text_of(chunks) == PLAIN
    assert log[0][1] == "primary:unreachable,backup:ok"


def test_a_request_the_primary_rejects_as_invalid_is_not_retried() -> None:
    with running_llm_stack() as stack:
        stack.primary.behavior = Behavior(status=400)
        reply = httpx.post(stack.url, json=chat(), headers=stack.headers)
        backup_calls = len(stack.backup.requests)
        entries = stack.rows("SELECT count(*) FROM window_entries")
    assert reply.status_code == 400
    assert reply.json()["error"]["code"] == "invalid_parameters"
    assert backup_calls == 0
    assert entries == [(0,)]


def test_both_failing_gives_one_clean_503_and_releases_the_reservation() -> None:
    with running_llm_stack() as stack:
        stack.primary.behavior = Behavior(status=500)
        stack.backup.behavior = Behavior(status=502)
        replies = [
            httpx.post(stack.url, json=chat(stream=stream), headers=stack.headers)
            for stream in (True, False)
        ]
        log = stack.logged(2)
        entries = stack.rows("SELECT count(*) FROM window_entries")
    for reply in replies:
        assert reply.status_code == 503
        error = reply.json()["error"]
        assert set(error) == {"type", "code", "message", "request_id"}
        assert error["type"] == "upstream_unavailable"
        assert error["request_id"] == reply.headers["x-request-id"]
        for leak in LEAKS:
            assert leak not in reply.text
    assert log[0] == (
        "none",
        "primary:status_500,backup:status_502",
        "upstream_unavailable",
        503,
        None,
    )
    assert entries == [(0,)]


def test_an_exception_inside_the_router_reaches_the_client_as_a_plain_500(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def explode(self: Provider, body: dict[str, Any]) -> httpx.Response:
        raise RuntimeError(f"no route at /srv/inference/router/dispatch.py: {LEAKY_ERROR}")

    monkeypatch.setattr(Provider, "open", explode)
    caplog.set_level(logging.ERROR, logger="llm_gateway")
    with running_llm_stack() as stack:
        reply = httpx.post(stack.url, json=chat(), headers=stack.headers)
        entries = stack.rows("SELECT count(*) FROM window_entries")
    assert reply.status_code == 500
    assert reply.json()["error"]["type"] == "internal_error"
    for leak in LEAKS:
        assert leak not in reply.text
    assert "RuntimeError" in caplog.text
    assert entries == [(0,)]


def test_a_stream_that_breaks_ends_with_one_error_event_and_no_failover() -> None:
    with running_llm_stack() as stack:
        stack.primary.behavior = Behavior(text=PLAIN, break_after=4)
        with httpx.stream("POST", stack.url, json=chat(), headers=stack.headers) as response:
            lines = [line for line in response.iter_lines() if line.startswith("data:")]
        log = stack.logged()
        backup_calls = len(stack.backup.requests)
    last = json.loads(lines[-1][len("data:") :])
    assert last["error"]["type"] == "upstream_unavailable"
    assert "data: [DONE]" not in lines
    assert PLAIN.startswith(text_of(chunks_of(iter(lines[:-1]))))
    for leak in LEAKS:
        assert leak not in lines[-1]
    assert backup_calls == 0
    assert log[0][2] == "stream_broke"
