"""All four tasks against the mocks, one line per check.

    make demo       every task
    make task2      one task and its tests (task1 to task4)

The gateways run in this process and log to data/demo/gateways.log, where the MCP gateway's
audit lines are. The customer server runs as a stdio subprocess, as a desktop client runs it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sqlite3
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import httpx2
from mcp import Client, MCPError, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

from gateway_common.logs import configure
from gateway_common.serving import background_server
from llm_gateway.app import create_app as create_llm_gateway
from llm_gateway.settings import ProviderConfig
from llm_gateway.settings import Settings as LlmSettings
from llm_gateway.tenants import add_tenant
from mcp_gateway.app import create_app as create_mcp_gateway
from mcp_gateway.auth import mint_token
from mcp_gateway.settings import Settings as McpSettings
from mock_downstream.app import CallLog, build_app
from mock_providers.app import Behavior, MockProvider

OUT = Path("data/demo")
REFUND = {"customer_id": "CUST-10001", "amount": 25.0, "reason": "Arrived broken, photos sent"}
CARD = "4111 1111 1111 1111"
EMAIL = "ada.lindqvist@example.com"


def say(task: int, line: str) -> None:
    print(f"task {task}  {line}", flush=True)


async def task1() -> None:
    server = StdioServerParameters(
        command=sys.executable, args=["-m", "customer_server"], env={"LOG_LEVEL": "WARNING"}
    )
    async with Client(server) as client:
        done = await client.call_tool("trigger_refund", REFUND)
        refund_id = (done.structured_content or {}).get("refund_id")
        say(1, f"refund of 25.00 for CUST-10001 accepted as {refund_id}")
        try:
            await client.call_tool("trigger_refund", {**REFUND, "amount": 25.001})
        except MCPError as refused:
            rules = ", ".join(f"{e['field']} {e['rule']}" for e in refused.data["errors"])
            say(1, f"refund of 25.001 refused with {refused.code} {refused.message}: {rules}")


async def call_admin_tool(url: str, token: str) -> str:
    async with (
        httpx2.AsyncClient(headers={"authorization": f"Bearer {token}"}) as http,
        Client(streamable_http_client(url, http_client=http)) as client,
    ):
        try:
            result = await client.call_tool("admin_reset_key", {"key_id": "key-1"})
        except MCPError as refused:
            return f"{refused.code} {refused.message}"
        return "a tool error" if result.is_error else "allowed"


def task2() -> None:
    log = CallLog()
    downstream_token = secrets.token_urlsafe(24)
    with background_server(build_app(log, downstream_token)) as downstream:
        settings = McpSettings(
            jwt_secret=secrets.token_hex(32),
            downstream_url=f"{downstream}/mcp",
            downstream_token=downstream_token,
        )
        with background_server(create_mcp_gateway(settings)) as gateway:
            for role in ("viewer", "admin"):
                token = mint_token(settings, role, f"demo-{role}", 300)
                outcome = asyncio.run(call_admin_tool(f"{gateway}/mcp", token))
                reached = sum(1 for entry in log.entries() if entry.tool == "admin_reset_key")
                say(2, f"{role} calls admin_reset_key: {outcome}, calls on the server {reached}")


@dataclass
class LlmStack:
    url: str
    primary: MockProvider
    primary_url: str
    headers: dict[str, str]
    db_path: str


@contextmanager
def llm_stack() -> Iterator[LlmStack]:
    primary, backup = MockProvider("primary"), MockProvider("backup")
    db_path = OUT / "llm_gateway.db"
    for stale in OUT.glob("llm_gateway.db*"):
        stale.unlink()
    with (
        background_server(primary.app()) as primary_url,
        background_server(backup.app()) as backup_url,
    ):
        settings = LlmSettings(
            secret=secrets.token_hex(32),
            primary=ProviderConfig("primary", f"{primary_url}/v1"),
            backup=ProviderConfig("backup", f"{backup_url}/v1"),
            db_path=str(db_path),
        )
        key = asyncio.run(add_tenant(settings.db_path, settings.secret, "demo"))
        with background_server(create_llm_gateway(settings)) as gateway:
            headers = {"authorization": f"Bearer {key}"}
            url = f"{gateway}/v1/chat/completions"
            yield LlmStack(url, primary, primary_url, headers, settings.db_path)


def chat(**extra: Any) -> dict[str, Any]:
    question = [{"role": "user", "content": "Where is my refund?"}]
    return {"model": "mock-model", "messages": question, "stream": True, **extra}


def stream(url: str, body: dict[str, Any], headers: dict[str, str]) -> tuple[list[str], str]:
    """The content pieces of a streamed reply, and its request id."""
    pieces = []
    with httpx.stream("POST", url, json=body, headers=headers, timeout=30) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if line.startswith("data: {"):
                for choice in json.loads(line[6:]).get("choices", []):
                    pieces.append(choice["delta"].get("content") or "")
        return [piece for piece in pieces if piece], response.headers.get("x-request-id", "")


def pieces_holding(value: str, pieces: list[str]) -> list[str]:
    """The streamed pieces that carry part of value."""
    start = "".join(pieces).index(value)
    end, at, holding = start + len(value), 0, []
    for piece in pieces:
        if at < end and at + len(piece) > start:
            holding.append(piece)
        at += len(piece)
    return holding


def task3(stack: LlmStack) -> None:
    sent, _ = stream(f"{stack.primary_url}/v1/chat/completions", chat(), {})
    received, _ = stream(stack.url, chat(), stack.headers)
    card = pieces_holding(CARD, sent)
    email = pieces_holding(EMAIL, sent)
    say(3, f"the provider sent the card in {len(card)} chunks: {' '.join(map(repr, card))}")
    say(3, f"and the email in {len(email)}, one ending in {email[-2]!r} with {email[-1]!r} to come")
    say(3, f"the client got: {''.join(received)}")


def attempts(db_path: str, request_id: str) -> str:
    """The route a request took, from the gateway's request log."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with closing(sqlite3.connect(db_path)) as db:
            row = db.execute(
                "SELECT attempts FROM request_log WHERE request_id = ?", (request_id,)
            ).fetchone()
        if row:
            return str(row[0])
        time.sleep(0.05)
    return "not logged"


def task4(stack: LlmStack) -> None:
    stack.primary.behavior = Behavior(first_byte_delay=3.5)
    slow: dict[str, Any] = {}

    def run_slow() -> None:
        started = time.monotonic()
        _, slow["request_id"] = stream(stack.url, chat(max_tokens=40_000), stack.headers)
        slow["seconds"] = time.monotonic() - started

    runner = threading.Thread(target=run_slow)
    runner.start()
    time.sleep(0.5)
    limited = httpx.post(stack.url, json=chat(max_tokens=20_000), headers=stack.headers)
    runner.join()
    route = attempts(stack.db_path, slow["request_id"])
    seconds = slow["seconds"]
    say(4, f"primary silent for 3.5 s, dropped at 3 s, backup done at {seconds:.1f} s: {route}")
    retry = limited.headers.get("retry-after")
    status = limited.status_code
    say(4, f"while it held 40,000 tokens, a request for 20,000 got {status}, retry after {retry} s")
    too_big = httpx.post(stack.url, json=chat(max_tokens=60_000), headers=stack.headers)
    code = too_big.json()["error"]["code"]
    say(4, f"max_tokens 60,000 is more than a minute allows: {too_big.status_code} {code}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("task", nargs="?", choices=["task1", "task2", "task3", "task4"])
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "gateways.log").open("w") as log:
        configure("INFO", log)
        if args.task in (None, "task1"):
            asyncio.run(task1())
        if args.task in (None, "task2"):
            task2()
        if args.task in (None, "task3", "task4"):
            with llm_stack() as stack:
                if args.task in (None, "task3"):
                    task3(stack)
                if args.task in (None, "task4"):
                    task4(stack)
    print(f"logs: {OUT / 'gateways.log'}, request log: {OUT / 'llm_gateway.db'}")


if __name__ == "__main__":
    main()
