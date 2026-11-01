"""Server-sent events: an incremental parser, and an encoder that keeps every field in order."""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass, field

_LINE_END = re.compile(r"\r\n|\r|\n")


@dataclass
class Event:
    """One event as its (field, value) lines in order. A comment line has the field ''."""

    lines: list[tuple[str, str]] = field(default_factory=list)

    @property
    def data(self) -> str | None:
        values = [value for name, value in self.lines if name == "data"]
        return "\n".join(values) if values else None

    def with_data(self, data: str) -> Event:
        """A copy whose data lines are replaced by data, with every other line kept in place."""
        lines: list[tuple[str, str]] = []
        placed = False
        for name, value in self.lines:
            if name != "data":
                lines.append((name, value))
            elif not placed:
                lines.extend(("data", part) for part in data.split("\n"))
                placed = True
        if not placed:
            lines.extend(("data", part) for part in data.split("\n"))
        return Event(lines)

    def encode(self) -> bytes:
        text = "".join(
            f":{value}\n" if name == "" else f"{name}: {value}\n" for name, value in self.lines
        )
        return f"{text}\n".encode()


class EventTooLarge(Exception):
    """An event grew past the size limit before its blank line arrived."""


class SseParser:
    """Turns a byte stream into events. Lines may end in CRLF, LF or CR and split anywhere."""

    def __init__(self, max_event_chars: int = 1 << 20) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self._pending = ""
        self._lines: list[tuple[str, str]] = []
        self._size = 0
        self._max = max_event_chars

    def feed(self, chunk: bytes) -> list[Event]:
        self._pending += self._decoder.decode(chunk)
        return self._drain(final=False)

    def close(self) -> list[Event]:
        """Flush what is left. A last event without its blank line is still delivered."""
        self._pending += self._decoder.decode(b"", final=True)
        events = self._drain(final=True)
        if self._pending:
            self._take(self._pending)
            self._pending = ""
        if self._lines:
            events.append(Event(self._lines))
            self._lines = []
        return events

    def _drain(self, final: bool) -> list[Event]:
        events: list[Event] = []
        while match := _LINE_END.search(self._pending):
            # A CR at the very end may be the first half of a CRLF split across chunks.
            if match.group() == "\r" and match.end() == len(self._pending) and not final:
                break
            line = self._pending[: match.start()]
            self._pending = self._pending[match.end() :]
            if line:
                self._take(line)
            elif self._lines:
                events.append(Event(self._lines))
                self._lines = []
                self._size = 0
        if len(self._pending) + self._size > self._max:
            raise EventTooLarge(f"event longer than {self._max} characters")
        return events

    def _take(self, line: str) -> None:
        self._size += len(line)
        if self._size > self._max:
            raise EventTooLarge(f"event longer than {self._max} characters")
        if line.startswith(":"):
            self._lines.append(("", line[1:]))
            return
        name, colon, value = line.partition(":")
        if colon and value.startswith(" "):
            value = value[1:]
        self._lines.append((name, value))
