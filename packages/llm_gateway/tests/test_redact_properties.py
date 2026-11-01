"""The invariant: for any text and any way of cutting it, streamed output equals batch output."""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from llm_gateway.redact import MAX_MATCH, REDACTED, StreamRedactor, luhn, redact

FILLER_ALPHABETS = (
    "abcxyz ",
    "abc .,:;!?\n",
    "ab 0123456789-",
    "ab@.-_+%x 19",
    " -0123456789",
)


def with_check_digit(body: str) -> str:
    return next(body + d for d in "0123456789" if luhn(body + d))


@st.composite
def cards(draw: st.DrawFn) -> str:
    length = draw(st.integers(min_value=13, max_value=19))
    body = draw(st.text("0123456789", min_size=length - 1, max_size=length - 1))
    number = with_check_digit(body)
    if draw(st.booleans()):
        last = str((int(number[-1]) + draw(st.integers(1, 9))) % 10)
        number = number[:-1] + last
    separator = draw(st.sampled_from(["", " ", "-"]))
    return separator.join(number[i : i + 4] for i in range(0, len(number), 4))


@st.composite
def ssns(draw: st.DrawFn) -> str:
    area = draw(
        st.sampled_from(["123", "001", "078", "550", "665", "667", "899", "000", "666", "900"])
    )
    group = draw(st.sampled_from(["45", "01", "99", "00"]))
    serial = draw(st.sampled_from(["6789", "0001", "9999", "0000"]))
    first, second = draw(st.sampled_from(["-", " "])), draw(st.sampled_from(["-", " "]))
    return f"{area}{first}{group}{second}{serial}"


@st.composite
def emails(draw: st.DrawFn) -> str:
    local = draw(st.text("abz09._+-", min_size=1, max_size=draw(st.sampled_from([8, 70]))))
    labels = draw(st.lists(st.text("abz09-", min_size=1, max_size=8), min_size=1, max_size=5))
    tld = draw(st.sampled_from(["com", "io", "co", "c", "c0m", "museum"]))
    trailing = draw(st.sampled_from(["", ".", "x", "-"]))
    return f"{local}@{'.'.join(labels)}.{tld}{trailing}"


@st.composite
def texts(draw: st.DrawFn) -> tuple[str, list[tuple[int, int]]]:
    """A text with PII and near misses planted at random places, and where each one sits."""
    alphabet = draw(st.sampled_from(FILLER_ALPHABETS))
    parts: list[str] = []
    planted: list[tuple[int, int]] = []
    for _ in range(draw(st.integers(min_value=0, max_value=5))):
        parts.append(draw(st.text(alphabet, max_size=12)))
        value = draw(st.one_of(cards(), ssns(), emails()))
        start = sum(len(part) for part in parts)
        planted.append((start, start + len(value)))
        parts.append(value)
    parts.append(draw(st.text(alphabet, max_size=12)))
    return "".join(parts), planted


def stream(text: str, cuts: list[int]) -> str:
    redactor = StreamRedactor()
    out = []
    previous = 0
    for cut in [*sorted(set(cuts)), len(text)]:
        out.append(redactor.feed(text[previous:cut]))
        previous = cut
        assert redactor.held <= MAX_MATCH
    out.append(redactor.finish())
    return "".join(out)


PROPERTY = settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@PROPERTY
@given(case=texts(), data=st.data())
def test_any_chunking_gives_the_batch_result(
    case: tuple[str, list[tuple[int, int]]], data: st.DataObject
) -> None:
    text, _ = case
    cuts = data.draw(st.lists(st.integers(min_value=0, max_value=len(text)), max_size=20))
    assert stream(text, cuts) == redact(text)


@PROPERTY
@given(case=texts())
def test_one_character_chunks_give_the_batch_result(
    case: tuple[str, list[tuple[int, int]]],
) -> None:
    text, _ = case
    assert stream(text, list(range(len(text)))) == redact(text)


@settings(max_examples=60, deadline=None)
@given(case=texts())
def test_a_split_at_every_index_inside_every_planted_value(
    case: tuple[str, list[tuple[int, int]]],
) -> None:
    text, planted = case
    expected = redact(text)
    for start, end in planted:
        for cut in range(start + 1, end):
            assert stream(text, [cut]) == expected


@settings(max_examples=200, deadline=None)
@given(
    words=st.lists(st.text("abcdefg", min_size=1, max_size=8), min_size=1, max_size=20),
    data=st.data(),
)
def test_planted_valid_pii_between_spaces_never_survives(
    words: list[str], data: st.DataObject
) -> None:
    secrets = [
        with_check_digit(data.draw(st.text("0123456789", min_size=15, max_size=15))),
        f"{data.draw(st.sampled_from(['123', '550', '899']))}-45-6789",
        f"{data.draw(st.text('abcdef', min_size=1, max_size=10))}@example.com",
    ]
    text = " ".join(words + secrets + words)
    cuts = data.draw(st.lists(st.integers(min_value=0, max_value=len(text)), max_size=30))
    output = stream(text, cuts)
    assert output.count(REDACTED) == 3
    for secret in secrets:
        assert secret not in output
