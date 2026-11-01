"""PII redaction for text that arrives in pieces.

The output of the streaming redactor is always identical to redact() on the whole
text, however the text was cut. It releases text as soon as no match can start in it
or grow out of it, and holds back only the rest.
"""

from __future__ import annotations

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


def luhn(digits: str) -> bool:
    total = 0
    for position, char in enumerate(reversed(digits)):
        digit = ord(char) - 48
        if position % 2:
            digit = digit * 2 - 9 if digit > 4 else digit * 2
        total += digit
    return total % 10 == 0


def _is_pii(match: regex.Match[str]) -> bool:
    if match.lastgroup != "card":
        return True
    return luhn("".join(char for char in match.group() if char.isdigit()))


def redact(text: str, counts: Counter[str] | None = None) -> str:
    """Redact a whole text at once. The streaming redactor must always agree with this."""
    out: list[str] = []
    pos = 0
    while (match := PATTERN.search(text, pos)) is not None:
        if _is_pii(match):
            out += [text[pos : match.start()], REDACTED]
            if counts is not None:
                counts[match.lastgroup or "unknown"] += 1
            pos = match.end()
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
            if _is_pii(match):
                out += [text[pos : match.start()], REDACTED]
                self.counts[match.lastgroup or "unknown"] += 1
                pos = match.end()
            else:
                out.append(text[pos : match.start() + 1])
                pos = match.start() + 1
        # Keep one released character: the lookbehinds need to see it.
        cut = max(0, pos - LOOKBEHIND)
        self._text, self._pos = text[cut:], pos - cut
        return "".join(out)


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
