import json

import httpx

from gateway_common.serving import background_server
from mock_providers.app import DEFAULT_TEXT, Behavior, MockProvider, tokens

REQUEST = {"model": "m", "messages": [{"role": "user", "content": "three words here"}]}


def test_tokens_are_short_pieces_that_join_back_into_the_text() -> None:
    pieces = tokens(DEFAULT_TEXT)
    assert "".join(pieces) == DEFAULT_TEXT
    assert max(len(piece.strip()) for piece in pieces) <= 4


def test_streams_usage_on_request_and_stops_at_max_tokens() -> None:
    provider = MockProvider("p", Behavior(text="one two six ten"))
    body = {**REQUEST, "stream": True, "max_tokens": 3, "stream_options": {"include_usage": True}}
    with background_server(provider.app()) as url:
        reply = httpx.post(f"{url}/v1/chat/completions", json=body)
    data = [line[6:] for line in reply.text.splitlines() if line.startswith("data: ")]
    chunks = [json.loads(item) for item in data[:-1]]
    content = [c["choices"][0]["delta"].get("content") for c in chunks if c["choices"]]
    assert data[-1] == "[DONE]"
    assert "".join(text for text in content if text) == "one two six"
    assert chunks[-1]["usage"] == {"prompt_tokens": 3, "completion_tokens": 3, "total_tokens": 6}
    assert provider.requests[0].finished


def test_answers_429_with_retry_after_and_errors_with_a_leaky_page() -> None:
    provider = MockProvider("p", Behavior(status=429, retry_after=7))
    with background_server(provider.app()) as url:
        limited = httpx.post(f"{url}/v1/chat/completions", json=REQUEST)
        provider.behavior = Behavior(status=500)
        failed = httpx.post(f"{url}/v1/chat/completions", json=REQUEST)
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "7"
    assert failed.status_code == 500
    assert "Traceback" in failed.text
