import pytest

from gateway_common.sse import Event, EventTooLarge, SseParser

STREAM = (
    b": keepalive\r\n\r\n"
    b'event: message\r\ndata: {"a": 1}\r\n\r\n'
    b"data: first line\ndata: second line\n\n"
    b"id: 7\rdata: caf\xc3\xa9\r\r"
    b"data: [DONE]\n\n"
)


def parse_in_pieces(stream: bytes, cut: int) -> list[Event]:
    parser = SseParser()
    pieces = [stream[i : i + cut] for i in range(0, len(stream), cut)]
    events = [event for piece in pieces for event in parser.feed(piece)]
    return events + parser.close()


@pytest.mark.parametrize("cut", [1, 2, 3, 5, 8, 13, len(STREAM)])
def test_events_are_the_same_however_the_bytes_are_cut(cut: int) -> None:
    events = parse_in_pieces(STREAM, cut)
    assert [event.data for event in events] == [
        None,
        '{"a": 1}',
        "first line\nsecond line",
        "caf\u00e9",
        "[DONE]",
    ]
    assert events[0].lines == [("", " keepalive")]
    assert events[1].lines[0] == ("event", "message")
    assert events[3].lines[0] == ("id", "7")


def test_encoding_keeps_every_field_and_comment() -> None:
    events = parse_in_pieces(STREAM, len(STREAM))
    again = parse_in_pieces(b"".join(event.encode() for event in events), 1)
    assert [event.lines for event in again] == [event.lines for event in events]


def test_with_data_replaces_only_the_data_lines() -> None:
    event = Event([("event", "message"), ("data", "old"), ("data", "more"), ("id", "3")])
    assert event.with_data("new").lines == [("event", "message"), ("data", "new"), ("id", "3")]
    assert Event([("event", "ping")]).with_data("x").lines == [("event", "ping"), ("data", "x")]


def test_a_last_event_without_its_blank_line_is_still_delivered() -> None:
    parser = SseParser()
    assert parser.feed(b"data: tail") == []
    assert [event.data for event in parser.close()] == ["tail"]


def test_an_event_past_the_size_limit_is_refused() -> None:
    parser = SseParser(max_event_chars=16)
    with pytest.raises(EventTooLarge):
        parser.feed(b"data: " + b"x" * 64)
