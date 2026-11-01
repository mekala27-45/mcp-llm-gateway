from pathlib import Path

import httpx
import pytest
from llm_stack import chat, post_stream, running_llm_stack

from llm_gateway.estimate import TokenEstimator
from mock_providers.app import Behavior, tokens

PLAIN = "Your refund was approved and should arrive within three days."
PROMPT = chat()["messages"]


@pytest.mark.parametrize(
    "authorization",
    [None, "Bearer gk_not-a-key-we-issued", "Basic YWNtZTpzZWNyZXQ=", "Bearer"],
    ids=["missing", "unknown", "basic", "no_key"],
)
def test_a_request_without_a_known_key_gets_401(authorization: str | None) -> None:
    with running_llm_stack() as stack:
        headers = {"authorization": authorization} if authorization else {}
        reply = httpx.post(stack.url, json=chat(), headers=headers)
        forwarded = len(stack.primary.requests)
    assert reply.status_code == 401
    assert reply.headers["www-authenticate"].startswith("Bearer")
    assert reply.json()["error"]["type"] == "unauthorized"
    assert forwarded == 0


def test_two_authorization_headers_get_401() -> None:
    with running_llm_stack() as stack:
        twice = [("authorization", f"Bearer {stack.key}")] * 2
        reply = httpx.post(stack.url, json=chat(), headers=twice)
        forwarded = len(stack.primary.requests)
    assert reply.status_code == 401
    assert forwarded == 0


def test_the_provider_gets_the_gateway_key_and_never_the_tenant_key() -> None:
    with running_llm_stack() as stack:
        post_stream(stack.url, chat(), stack.headers)
        seen = stack.primary.requests[-1].authorization
    assert seen == "Bearer mock-primary-key"


def test_an_estimate_over_the_whole_limit_gets_its_own_413() -> None:
    with running_llm_stack() as stack:
        reply = httpx.post(stack.url, json=chat(max_tokens=50_000), headers=stack.headers)
        forwarded = len(stack.primary.requests)
    assert reply.status_code == 413
    assert reply.json()["error"]["code"] == "estimate_over_limit"
    assert "retry-after" not in reply.headers
    assert forwarded == 0


@pytest.mark.parametrize("value", [0, -5, "100", True, 1.5, None])
def test_max_tokens_must_be_a_positive_whole_number(value: object) -> None:
    with running_llm_stack() as stack:
        reply = httpx.post(stack.url, json=chat(max_tokens=value), headers=stack.headers)
    assert reply.status_code == 400
    assert reply.json()["error"]["code"] == "invalid_max_tokens"


def test_a_request_without_max_tokens_is_given_the_default_bound() -> None:
    with running_llm_stack() as stack:
        post_stream(stack.url, chat(), stack.headers)
        seen = stack.primary.requests[-1].max_tokens
    assert seen == 1024


def test_a_key_over_its_window_gets_429_with_retry_after() -> None:
    with running_llm_stack() as stack:
        stack.primary.behavior = Behavior(text=PLAIN, token_interval=0.02)
        first = chat(max_tokens=40_000)
        with httpx.stream("POST", stack.url, json=first, headers=stack.headers) as running:
            # The first stream holds a 40,000 token reservation until it ends.
            refused = httpx.post(stack.url, json=chat(max_tokens=20_000), headers=stack.headers)
            running.read()
        stack.logged(2)
        # Reconciled to what it really used, the first stream leaves room again.
        later = httpx.post(stack.url, json=chat(max_tokens=20_000), headers=stack.headers)
    assert refused.status_code == 429
    assert refused.json()["error"]["type"] == "rate_limited"
    assert 1 <= int(refused.headers["retry-after"]) <= 60
    assert later.status_code == 200


def test_the_reservation_is_reconciled_to_the_reported_usage() -> None:
    with running_llm_stack() as stack:
        stack.primary.behavior = Behavior(text=PLAIN)
        post_stream(stack.url, chat(max_tokens=500), stack.headers)
        stack.logged()
        entries = stack.rows("SELECT tokens, state FROM window_entries")
    words = len(PROMPT[0]["content"].split())
    assert entries == [(words + len(tokens(PLAIN)), "committed")]


@pytest.mark.parametrize("stream", [True, False], ids=["stream", "whole"])
def test_without_reported_usage_the_counted_output_is_charged(stream: bool) -> None:
    with running_llm_stack() as stack:
        stack.primary.behavior = Behavior(text=PLAIN, report_usage=False)
        httpx.post(stack.url, json=chat(stream=stream), headers=stack.headers)
        stack.logged()
        entries = stack.rows("SELECT tokens, state FROM window_entries")
    counted = TokenEstimator(use_tiktoken=False).prompt(PROMPT) + len(PLAIN.encode())
    assert entries == [(counted, "committed")]


@pytest.mark.parametrize("asked", [True, False])
def test_the_usage_chunk_reaches_only_a_client_that_asked_for_it(asked: bool) -> None:
    with running_llm_stack() as stack:
        body = chat(stream_options={"include_usage": True}) if asked else chat()
        chunks = post_stream(stack.url, body, stack.headers)
    assert len([chunk for chunk in chunks if chunk.get("usage")]) == (1 if asked else 0)


def test_a_client_that_hangs_up_is_charged_for_what_it_received() -> None:
    with running_llm_stack() as stack:
        stack.primary.behavior = Behavior(text=PLAIN, repeat=50, token_interval=0.01)
        with httpx.stream("POST", stack.url, json=chat(), headers=stack.headers) as response:
            next(response.iter_lines())
        log = stack.logged()
        entries = stack.rows("SELECT state FROM window_entries")
    assert log[0][2] == "client_closed"
    assert entries == [("committed",)]


def test_the_request_log_keeps_the_route_and_never_the_content() -> None:
    with running_llm_stack() as stack:
        post_stream(stack.url, chat(), stack.headers)
        log = stack.logged()
        files = Path(stack.settings.db_path).parent.glob("gateway.db*")
        stored = b"".join(path.read_bytes() for path in files)
        key = stack.key
    assert log[0][:4] == ("primary", "primary:ok", "ok", 200)
    for content in (PROMPT[0]["content"], "ada.lindqvist", "4111 1111", "123-45-6789", key):
        assert content.encode() not in stored
