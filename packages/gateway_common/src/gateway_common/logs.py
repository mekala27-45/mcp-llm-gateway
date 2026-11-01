"""JSON log lines on stderr, one object per line, tagged with the current request id."""

from __future__ import annotations

import json
import logging
import sys
import time
from contextvars import ContextVar
from typing import Any, TextIO

request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


class JsonFormatter(logging.Formatter):
    """Formats a record as a single line of JSON."""

    def format(self, record: logging.LogRecord) -> str:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
        entry: dict[str, Any] = {
            "ts": f"{stamp}.{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        current = request_id.get()
        if current is not None:
            entry["request_id"] = current
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            entry.update(fields)
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        # ensure_ascii keeps control and zero width characters escaped, so a
        # hostile value cannot break the line or hide inside it.
        return json.dumps(entry, ensure_ascii=True, default=str)


def configure(level: str = "INFO", stream: TextIO | None = None) -> None:
    """Send every log record in the process to stderr, or to stream, as JSON."""
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "httpx2", "httpcore2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log_event(
    logger: logging.Logger, msg: str, /, level: int = logging.INFO, **fields: Any
) -> None:
    """Log one message with structured fields merged into the JSON line."""
    logger.log(level, msg, extra={"fields": fields})
