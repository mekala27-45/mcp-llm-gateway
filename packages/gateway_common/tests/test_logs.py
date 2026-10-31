import io
import json
import logging
from collections.abc import Iterator

import pytest

from gateway_common.logs import JsonFormatter, configure, log_event, request_id


@pytest.fixture
def stream_logger() -> Iterator[tuple[logging.Logger, io.StringIO]]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("gateway_common.tests")
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    yield logger, stream
    logger.handlers.clear()


def test_line_is_one_json_object_with_request_id_and_fields(
    stream_logger: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = stream_logger
    token = request_id.set("req-1")
    try:
        log_event(logger, "decision", tool="get_customer_record", decision="forward")
    finally:
        request_id.reset(token)

    line = stream.getvalue()
    assert line.count("\n") == 1
    entry = json.loads(line)
    assert entry["msg"] == "decision"
    assert entry["level"] == "info"
    assert entry["request_id"] == "req-1"
    assert entry["tool"] == "get_customer_record"
    assert entry["decision"] == "forward"


def test_traceback_stays_on_one_line(stream_logger: tuple[logging.Logger, io.StringIO]) -> None:
    logger, stream = stream_logger
    try:
        _ = 1 / 0
    except ZeroDivisionError:
        logger.exception("handler failed")

    line = stream.getvalue()
    assert line.count("\n") == 1
    assert "ZeroDivisionError" in json.loads(line)["exc"]


def test_control_and_zero_width_characters_are_escaped(
    stream_logger: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = stream_logger
    log_event(logger, "decision", tool="admin​_reset\nkey")

    line = stream.getvalue()
    assert line.count("\n") == 1
    assert "\\u200b" in line
    assert json.loads(line)["tool"] == "admin​_reset\nkey"


def test_configure_sends_records_to_stderr_only(capsys: pytest.CaptureFixture[str]) -> None:
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        configure("INFO")
        logging.getLogger("service").info("started")
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["msg"] == "started"
