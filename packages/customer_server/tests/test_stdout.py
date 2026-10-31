"""Reads every byte the server writes to stdout and checks that each line is JSON-RPC."""

import json
import os
import queue
import subprocess
import sys
import threading
from collections.abc import Iterator
from typing import IO, Any

import pytest
from mcp.types import (
    CLIENT_CAPABILITIES_META_KEY,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    LATEST_PROTOCOL_VERSION,
    PROTOCOL_VERSION_META_KEY,
    jsonrpc_message_adapter,
)

ENVELOPE = {PROTOCOL_VERSION_META_KEY: LATEST_PROTOCOL_VERSION, CLIENT_CAPABILITIES_META_KEY: {}}
REFUND = {"customer_id": "CUST-10001", "reason": "Duplicate charge on the October invoice"}


def request(request_id: int, method: str, **params: Any) -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": {**params, "_meta": ENVELOPE},
        }
    )


def refund_with_raw_amount(request_id: int, amount_token: str) -> str:
    # json.dumps cannot write NaN as a bare token, so the amount is spliced in as text.
    line = request(
        request_id, "tools/call", name="trigger_refund", arguments={**REFUND, "amount": 0}
    )
    return line.replace('"amount": 0', f'"amount": {amount_token}')


class RawServer:
    def __init__(self) -> None:
        env = {**os.environ, "CUSTOMER_SERVER_TEST_TOOLS": "1", "LOG_LEVEL": "INFO"}
        self.process = subprocess.Popen(
            [sys.executable, "-m", "customer_server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        self.lines: queue.Queue[bytes] = queue.Queue()
        self.stderr = bytearray()
        assert self.process.stdout is not None
        assert self.process.stderr is not None
        threading.Thread(target=self._read_lines, args=(self.process.stdout,), daemon=True).start()
        threading.Thread(target=self._read_err, args=(self.process.stderr,), daemon=True).start()

    def _read_lines(self, pipe: IO[bytes]) -> None:
        for line in pipe:
            self.lines.put(line)

    def _read_err(self, pipe: IO[bytes]) -> None:
        for chunk in iter(lambda: pipe.read(4096), b""):
            self.stderr.extend(chunk)

    def send(self, line: str) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(line.encode() + b"\n")
        self.process.stdin.flush()

    def ask(self, line: str) -> bytes:
        self.send(line)
        return self.lines.get(timeout=15)

    def finish(self) -> list[bytes]:
        assert self.process.stdin is not None
        self.process.stdin.close()
        self.process.wait(timeout=15)
        rest = []
        while not self.lines.empty():
            rest.append(self.lines.get())
        return rest


@pytest.fixture
def server() -> Iterator[RawServer]:
    raw = RawServer()
    yield raw
    if raw.process.poll() is None:
        raw.process.kill()


def reject_constant(token: str) -> None:
    raise ValueError(f"the server wrote a bare {token}")


def test_stdout_carries_only_json_rpc_even_with_planted_writes(server: RawServer) -> None:
    replies = [
        server.ask(request(1, "server/discover")),
        server.ask(request(2, "tools/list")),
        server.ask(request(3, "tools/call", name="debug_stdout_noise", arguments={})),
    ]
    server.send("this line is not json")
    nan_reply = server.ask(refund_with_raw_amount(4, "NaN"))
    infinity_reply = server.ask(refund_with_raw_amount(5, "Infinity"))
    crash_reply = server.ask(request(6, "tools/call", name="debug_crash", arguments={}))
    replies += [nan_reply, infinity_reply, crash_reply]
    replies.append(
        server.ask(
            request(
                7, "tools/call", name="get_customer_record", arguments={"customer_id": "CUST-10001"}
            )
        )
    )
    replies += server.finish()

    for line in replies:
        assert line.endswith(b"\n")
        message = json.loads(line, parse_constant=reject_constant)
        assert message["jsonrpc"] == "2.0"
        jsonrpc_message_adapter.validate_json(line)
    assert [json.loads(line)["id"] for line in replies] == [1, 2, 3, 4, 5, 6, 7]

    stdout_bytes = b"".join(replies)
    assert b"stray" not in stdout_bytes
    assert b"stray print from a tool handler" in server.stderr
    assert b"stray os.write to descriptor 1" in server.stderr
    assert b"stray print from a child process" in server.stderr


def test_nan_and_infinity_amounts_are_invalid_params(server: RawServer) -> None:
    for request_id, token in ((1, "NaN"), (2, "Infinity"), (3, "-Infinity")):
        error = json.loads(server.ask(refund_with_raw_amount(request_id, token)))["error"]
        assert error["code"] == INVALID_PARAMS
        assert {"field": "amount", "rule": "finite_number"} in [
            {"field": e["field"], "rule": e["rule"]} for e in error["data"]["errors"]
        ]
    server.finish()


def test_an_unexpected_failure_leaves_the_traceback_on_stderr_only(server: RawServer) -> None:
    reply = server.ask(request(1, "tools/call", name="debug_crash", arguments={}))
    server.finish()
    assert json.loads(reply)["error"] == {"code": INTERNAL_ERROR, "message": "Internal error"}
    assert b"/srv/customer_server" not in reply
    assert b"Traceback" in server.stderr
