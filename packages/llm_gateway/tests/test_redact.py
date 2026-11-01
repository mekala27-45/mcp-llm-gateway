import pytest

from llm_gateway.redact import MAX_MATCH, REDACTED, StreamRedactor, luhn, redact


def streamed(pieces: list[str]) -> str:
    redactor = StreamRedactor()
    return "".join(redactor.feed(piece) for piece in pieces) + redactor.finish()


@pytest.mark.parametrize(
    "value",
    [
        "ada.lindqvist@example.com",
        "first_last+tag@mail.example.co.uk",
        "4111 1111 1111 1111",
        "4111-1111-1111-1111",
        "4111111111111111",
        "378282246310005",
        "6011 1111 1111 1117",
        "123-45-6789",
        "123 45 6789",
        "001-01-0001",
        "899-99-9999",
    ],
)
def test_pii_is_replaced_by_the_marker(value: str) -> None:
    assert redact(f"see {value} here") == f"see {REDACTED} here"


@pytest.mark.parametrize(
    "value",
    [
        "4111 1111 1111 1112",
        "000-12-3456",
        "666-12-3456",
        "900-12-3456",
        "123-00-4567",
        "123-45-0000",
        "123456789",
        "1234 5678 9012",
        "user@localhost",
        "user@example.c0m",
        "12345678901234567890",
    ],
    ids=[
        "card failing luhn",
        "ssn area 000",
        "ssn area 666",
        "ssn area 9xx",
        "ssn group 00",
        "ssn serial 0000",
        "ssn without separators",
        "twelve digits",
        "host without a dot",
        "digit in the top level domain",
        "twenty digits",
    ],
)
def test_look_alikes_are_left_alone(value: str) -> None:
    assert redact(f"see {value} here") == f"see {value} here"


def test_luhn_accepts_test_numbers_and_rejects_one_digit_off() -> None:
    assert luhn("4111111111111111")
    assert luhn("378282246310005")
    assert not luhn("4111111111111112")


def test_card_split_across_chunks_is_redacted() -> None:
    assert streamed(["pay 4111 11", "11 1111 1", "111 now"]) == f"pay {REDACTED} now"


def test_email_split_across_chunks_is_redacted() -> None:
    pieces = ["mail ada.", "lindq", "vist@exa", "mple.", "com today"]
    assert streamed(pieces) == f"mail {REDACTED} today"


def test_a_complete_email_at_the_end_of_the_buffer_is_held() -> None:
    redactor = StreamRedactor()
    assert redactor.feed("mail john@example.co") == "mail "
    assert redactor.feed("m soon") == f"{REDACTED} "
    assert redactor.finish() == "soon"


def test_thirteen_luhn_digits_at_the_end_are_held_while_they_can_grow() -> None:
    assert luhn("4111111111119")
    assert luhn("4111111111119007")
    redactor = StreamRedactor()
    assert redactor.feed("card 4111111111119") == "card "
    assert redactor.held == 13
    assert redactor.feed("007 ok") == f"{REDACTED} "
    assert redactor.finish() == "ok"


def test_an_ssn_at_the_end_is_held_until_the_next_character() -> None:
    redactor = StreamRedactor()
    assert redactor.feed("ssn 123-45-6789") == "ssn "
    assert redactor.feed("0 is not one") == "123-45-67890 is not "
    assert redactor.finish() == "one"


def test_a_failed_card_candidate_does_not_hide_an_ssn_inside_it() -> None:
    assert not luhn("9999123456789")
    assert redact("ref 9999 123-45-6789") == f"ref 9999 {REDACTED}"


def test_counts_each_kind_and_never_keeps_the_values() -> None:
    redactor = StreamRedactor()
    redactor.feed("a@example.com and 4111111111111111 and 123-45-6789 and b@example.org.")
    redactor.finish()
    assert redactor.counts == {"email": 2, "card": 1, "ssn": 1}


def test_the_hold_never_exceeds_the_longest_possible_match() -> None:
    redactor = StreamRedactor()
    candidate = "a" * 60 + "@" + ".".join(["b" * 63] * 4) + "." + "c" * 40
    for char in candidate * 3:
        redactor.feed(char)
        assert redactor.held <= MAX_MATCH
    redactor.finish()
    assert 300 < redactor.largest_hold <= MAX_MATCH
