"""PII redaction for text that arrives in pieces.

The output of the streaming redactor is always identical to redact() on the whole
text, however the text was cut. It releases text as soon as no match can start in it
or grow out of it, and holds back only the rest.
"""

from __future__ import annotations

import re
import string
from collections import Counter

import regex

REDACTED = "[REDACTED]"

# Every quantifier is bounded, so no candidate can be longer than MAX_MATCH. That is
# what bounds the holdback, and it is also why the patterns run in linear time.
LOCAL_MAX, LABEL_MAX, LABELS_MAX, TLD_MAX = 64, 63, 4, 24
# The local part starts where a run of local part characters starts. Without that,
# every character inside a long run would be a fresh start, and a stream cut into
# single characters would cost the square of the run length.
LOCAL = r"[A-Za-z0-9._%+_-]"
LOCAL_CHARS = string.ascii_letters + string.digits + "._%+_-"
EMAIL = (
    rf"(?<!{LOCAL}){LOCAL}{{1,{LOCAL_MAX}}}+@"
    rf"(?:[A-Za-z0-9-]{{1,{LABEL_MAX}}}+\.){{1,{LABELS_MAX}}}[A-Za-z]{{2,{TLD_MAX}}}+"
)
EMAIL_MAX = LOCAL_MAX + 1 + LABELS_MAX * (LABEL_MAX + 1) + TLD_MAX

# Area 001 to 899 without 666, group 01 to 99, serial 0001 to 9999.
SSN = (
    r"(?<![0-9])"
    r"(?:00[1-9]|0[1-9][0-9]|[1-578][0-9]{2}|6[0-57-9][0-9]|66[0-57-9])[ -]"
    r"(?:0[1-9]|[1-9][0-9])[ -]"
    r"(?:000[1-9]|00[1-9][0-9]|0[1-9][0-9]{2}|[1-9][0-9]{3})"
    r"(?![0-9])"
)
SSN_MAX = 11

# 13 to 19 digits, a single space or dash allowed after any digit. Luhn is checked in code.
CARD = r"(?<![0-9])(?:[0-9][ -]?){12,18}[0-9](?![0-9])"
CARD_MAX = 19 + 18

MAX_MATCH = max(EMAIL_MAX, SSN_MAX, CARD_MAX)
LOOKBEHIND = 1

KINDS = ("email", "ssn", "card")
PATTERN = regex.compile(f"(?P<email>{EMAIL})|(?P<ssn>{SSN})|(?P<card>{CARD})")

# The same alternatives with a failing tail, so no match can ever complete. A partial
# search then reports every place where some path of the pattern ran into the end of
# the text, which is exactly where more input could still change the result. The
# plain partial search is not enough: it prefers any complete match it can find,
# even a shorter one, over a longer match that is still growing.
_SOURCES = {"email": EMAIL, "ssn": SSN, "card": CARD}
_PENDING = {
    kind: regex.compile(
        "(?:" + "|".join(f"(?:{_SOURCES[k]})" for k in KINDS[: KINDS.index(kind) + 1]) + ")(?!)"
    )
    for kind in KINDS
}
_ANY_PENDING = _PENDING[KINDS[-1]]

# Every pattern needs an @ or a digit, so text with neither holds no match.
_SIGNAL = re.compile(r"[@0-9]")


_DOUBLED = (0, 2, 4, 6, 8, 1, 3, 5, 7, 9)


def luhn(digits: str) -> bool:
    total = 0
    for position, char in enumerate(reversed(digits)):
        digit = ord(char) - 48
        total += _DOUBLED[digit] if position % 2 else digit
    return total % 10 == 0


def _pii_end(match: regex.Match[str]) -> int | None:
    """Where the PII in a match ends, or None when the match is not PII after all."""
    if match.lastgroup != "card":
        return match.end()
    # The regex takes as many digits as it can, so a card followed by a space and
    # more digits arrives as one longer candidate. Prefixes that end before a
    # separator are tried shortest first, so the card is found and the digits after
    # it (often an SSN) are left for the next match instead of being swallowed.
    # The Luhn sum of each prefix comes from running sums kept by digit parity.
    candidate = match.group()
    plain, doubled, count = [0, 0], [0, 0], 0
    for offset, char in enumerate(candidate):
        if char == " " or char == "-":
            continue
        digit = ord(char) - 48
        count += 1
        plain[count % 2] += digit
        doubled[count % 2] += _DOUBLED[digit]
        boundary = offset + 1 == len(candidate) or candidate[offset + 1] in " -"
        if count >= 13 and boundary and (plain[count % 2] + doubled[1 - count % 2]) % 10 == 0:
            return match.start() + offset + 1
    return None


def redact(text: str, counts: Counter[str] | None = None) -> str:
    """Redact a whole text at once. The streaming redactor must always agree with this."""
    out: list[str] = []
    pos = 0
    while (match := PATTERN.search(text, pos)) is not None:
        if (end := _pii_end(match)) is not None:
            out += [text[pos : match.start()], REDACTED]
            if counts is not None:
                counts[match.lastgroup or "unknown"] += 1
            pos = end
        else:
            # Not a card after all. Look again one character later, so a real
            # SSN or card inside a failed candidate is still found.
            out.append(text[pos : match.start() + 1])
            pos = match.start() + 1
    out.append(text[pos:])
    return "".join(out)


class StreamRedactor:
    """Redacts PII in text that arrives in pieces, holding back as little as it can."""

    def __init__(self) -> None:
        self._text = ""
        self._pos = 0
        self.counts: Counter[str] = Counter()
        self.largest_hold = 0

    @property
    def held(self) -> int:
        """Characters received but not released yet."""
        return len(self._text) - self._pos

    def feed(self, piece: str) -> str:
        self._text += piece
        out = self._release(final=False)
        self.largest_hold = max(self.largest_hold, self.held)
        return out

    def finish(self) -> str:
        """Release everything that is left, by the same rules."""
        return self._release(final=True)

    def _release(self, final: bool) -> str:
        text, pos, out = self._text, self._pos, []
        end = len(text)
        if _SIGNAL.search(text, pos) is None:
            return self._release_plain(final)
        pending = end if final else _earliest_pending(text, pos)
        while pos < end:
            if pending < pos:
                pending = _earliest_pending(text, pos)
            if pending == pos:
                break
            match = PATTERN.search(text, pos)
            if match is None or match.start() > pending:
                out.append(text[pos:pending])
                pos = pending
                break
            if not final and _may_change(text, match, pending):
                out.append(text[pos : match.start()])
                pos = match.start()
                break
            if (pii_end := _pii_end(match)) is not None:
                out += [text[pos : match.start()], REDACTED]
                self.counts[match.lastgroup or "unknown"] += 1
                pos = pii_end
            else:
                out.append(text[pos : match.start() + 1])
                pos = match.start() + 1
        # Keep one released character: the lookbehinds need to see it.
        cut = max(0, pos - LOOKBEHIND)
        self._text, self._pos = text[cut:], pos - cut
        return "".join(out)

    def _release_plain(self, final: bool) -> str:
        # The fast path for text with no @ and no digit, which is most of a stream.
        # Only the start of an email still waiting for its @ can be pending: a run of
        # local part characters that reaches the end and is not longer than a local part.
        text, pos = self._text, self._pos
        run = len(text) - len(text.rstrip(LOCAL_CHARS))
        start = len(text) - run
        hold = start if not final and start >= pos and 0 < run <= LOCAL_MAX else len(text)
        released = text[pos:hold]
        cut = max(0, hold - LOOKBEHIND)
        self._text, self._pos = text[cut:], hold - cut
        return released


def _earliest_pending(text: str, pos: int) -> int:
    found = _ANY_PENDING.search(text, pos, partial=True)
    return len(text) if found is None else found.start()


def _may_change(text: str, match: regex.Match[str], pending: int) -> bool:
    # A complete match that touches the end of the text is still held: an email
    # ending in .co may become .com, and 13 digits that pass Luhn may be the first
    # 13 of 16, so redacting now would send the last three digits in clear.
    if match.end() == len(text):
        return True
    if match.start() < pending:
        return False
    # Something is still growing where this match starts. It only matters if it is
    # this alternative or one tried before it.
    kind = match.lastgroup or KINDS[-1]
    return _PENDING[kind].match(text, match.start(), partial=True) is not None
