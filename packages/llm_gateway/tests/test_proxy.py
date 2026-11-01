import time

import httpx
from llm_stack import chat, chunks_of, running_llm_stack, text_of

from mock_providers.app import Behavior

PLAIN = "Your refund was approved this morning and should arrive within three days."


def test_streams_chunks_as_they_arrive() -> None:
    with running_llm_stack() as stack:
        stack.primary.behavior = Behavior(text=PLAIN, token_interval=0.02)
        started = time.monotonic()
        first_content_at = None
        with httpx.stream("POST", stack.url, json=chat(), timeout=30) as response:
            lines = []
            for line in response.iter_lines():
                lines.append(line)
                if first_content_at is None and '"content": "Your' in line:
                    first_content_at = time.monotonic() - started
        total = time.monotonic() - started

    assert text_of(chunks_of(iter(lines))) == PLAIN
    assert first_content_at is not None
    assert first_content_at < total / 3


def test_a_non_streaming_request_gets_the_provider_json() -> None:
    with running_llm_stack() as stack:
        stack.primary.behavior = Behavior(text=PLAIN)
        reply = httpx.post(stack.url, json=chat(stream=False))
    assert reply.status_code == 200
    assert reply.json()["choices"][0]["message"]["content"] == PLAIN


def test_a_provider_error_becomes_a_clean_503() -> None:
    with running_llm_stack() as stack:
        stack.primary.behavior = Behavior(status=500)
        stack.backup.behavior = Behavior(status=500)
        reply = httpx.post(stack.url, json=chat())
    assert reply.status_code == 503
    error = reply.json()["error"]
    assert error["type"] == "upstream_unavailable"
    assert error["request_id"] == reply.headers["x-request-id"]
    for leak in ("Traceback", "inference-node", "/srv/", "primary", "backup", "127.0.0.1"):
        assert leak not in reply.text


def test_invalid_json_gets_invalid_request_in_the_common_shape() -> None:
    with running_llm_stack() as stack:
        reply = httpx.post(stack.url, content=b"{not json")
    assert reply.status_code == 400
    assert set(reply.json()["error"]) == {"type", "code", "message", "request_id"}
    assert reply.json()["error"]["type"] == "invalid_request"


def test_an_unknown_route_has_the_same_error_shape() -> None:
    with running_llm_stack() as stack:
        reply = httpx.get(stack.url.replace("/chat/completions", "/models"))
    assert reply.status_code == 404
    assert set(reply.json()["error"]) == {"type", "code", "message", "request_id"}


def test_a_body_over_the_limit_is_request_too_large() -> None:
    with running_llm_stack(max_body_bytes=1024) as stack:
        reply = httpx.post(stack.url, json=chat(padding="x" * 2048))
    assert reply.status_code == 413
    assert reply.json()["error"]["type"] == "request_too_large"


def test_a_client_disconnect_cancels_the_upstream_stream() -> None:
    with running_llm_stack() as stack:
        stack.primary.behavior = Behavior(text=PLAIN, repeat=50, token_interval=0.01)
        with httpx.stream("POST", stack.url, json=chat(), timeout=30) as response:
            next(response.iter_lines())
        deadline = time.monotonic() + 5
        while not stack.primary.requests[-1].disconnected and time.monotonic() < deadline:
            time.sleep(0.05)
        seen = stack.primary.requests[-1]
    assert seen.disconnected
    assert not seen.finished


def test_chunk_fields_are_kept_and_the_stream_ends_with_done() -> None:
    with running_llm_stack() as stack:
        stack.primary.behavior = Behavior(text=PLAIN)
        with httpx.stream("POST", stack.url, json=chat(), timeout=30) as response:
            lines = [line for line in response.iter_lines() if line]
    chunks = chunks_of(iter(lines))
    assert lines[-1] == "data: [DONE]"
    assert {chunk["system_fingerprint"] for chunk in chunks} == {"fp_primary"}
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert text_of(chunks) == PLAIN
