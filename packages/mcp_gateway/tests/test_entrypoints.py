"""The deployable commands: the token minter, the mock server and the gateway, as processes."""

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
from gateway_stack import DOWNSTREAM_TOKEN, SECRET, headers_for, rpc


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@contextmanager
def running(module: str, port: int, env: dict[str, str]) -> Iterator[None]:
    process = subprocess.Popen(
        [sys.executable, "-m", module, "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_the_gateway_runs_from_its_commands() -> None:
    downstream_port, gateway_port = free_port(), free_port()
    env = {
        **os.environ,
        "GATEWAY_JWT_SECRET": SECRET,
        "GATEWAY_DOWNSTREAM_TOKEN": DOWNSTREAM_TOKEN,
        "GATEWAY_DOWNSTREAM_URL": f"http://127.0.0.1:{downstream_port}/mcp",
        "LOG_LEVEL": "WARNING",
    }
    minted = subprocess.run(
        [sys.executable, "-m", "mcp_gateway.tokens", "--role", "viewer"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    body = rpc("tools/list")
    headers = headers_for(minted.stdout.strip(), body)
    url = f"http://127.0.0.1:{gateway_port}/mcp"
    with (
        running("mock_downstream", downstream_port, env),
        running("mcp_gateway", gateway_port, env),
    ):
        deadline = time.monotonic() + 20
        while True:
            try:
                response = httpx.post(url, json=body, headers=headers)
                if response.status_code == 200 or time.monotonic() > deadline:
                    break
            except httpx.TransportError:
                if time.monotonic() > deadline:
                    raise
            time.sleep(0.1)
    assert response.status_code == 200
    assert "admin_reset_key" in {tool["name"] for tool in response.json()["result"]["tools"]}
