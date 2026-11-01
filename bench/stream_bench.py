"""What streaming redaction costs: time to first token, delay per character, redactor
throughput, the largest hold, and gateway memory over a long stream.

    make bench          full run, writes results/bench.json
    make bench-quick    the short setting CI runs
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from llm_gateway.redact import StreamRedactor
from mock_providers.app import tokens

PROSE = (
    "Thanks for waiting. Your refund for the October invoice was approved this morning, and "
    "the money should reach your account within three working days. "
)
WITH_PII = (
    "Your refund is on its way. The receipt went to ada.lindqvist@example.com, the card "
    "4111 1111 1111 1111 was credited, and the SSN on file is 123-45-6789. "
)
SECRET = "bench-secret-0123456789abcdef0123456789"
TOKEN_INTERVAL_MS = 20


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def wait_for_port(port: int, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise TimeoutError(f"nothing listening on port {port}")


@contextmanager
def process(args: list[str], port: int, env: dict[str, str]) -> Iterator[subprocess.Popen[bytes]]:
    child = subprocess.Popen(
        [sys.executable, "-m", *args, "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for_port(port)
        yield child
    finally:
        child.terminate()
        child.wait(timeout=10)


def rss_mb(pid: int) -> float:
    status = Path(f"/proc/{pid}/status")
    if status.exists():
        line = next(x for x in status.read_text().splitlines() if x.startswith("VmRSS:"))
        return int(line.split()[1]) / 1024
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True)
    return int(out.stdout.strip()) / 1024


def percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(share * (len(ordered) - 1)))]


def machine() -> dict[str, Any]:
    cpu = platform.processor() or platform.machine()
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        names = [
            x.split(":", 1)[1].strip()
            for x in cpuinfo.read_text().splitlines()
            if x.startswith("model name")
        ]
        cpu = names[0] if names else cpu
    memory = None
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        total = next(x for x in meminfo.read_text().splitlines() if x.startswith("MemTotal:"))
        memory = round(int(total.split()[1]) / 1024 / 1024, 1)
    return {
        "cpu": cpu,
        "cores": os.cpu_count(),
        "memory_gb": memory,
        "os": platform.platform(),
        "python": platform.python_version(),
    }


def redactor_alone(megabytes: int) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for name, block in (("prose", PROSE), ("pii_every_150_chars", WITH_PII)):
        text = block * (megabytes * 1_000_000 // len(block))
        for size in (4, 16, 64):
            redactor = StreamRedactor()
            started = time.perf_counter()
            for start in range(0, len(text), size):
                redactor.feed(text[start : start + size])
            redactor.finish()
            elapsed = time.perf_counter() - started
            results[f"{name}_chunks_of_{size}"] = {
                "mb_per_second": round(len(text) / elapsed / 1e6, 2),
                "largest_hold_chars": redactor.largest_hold,
            }
    return results


def first_token(
    url: str, body: dict[str, Any], headers: dict[str, str]
) -> tuple[float, list[float]]:
    """Seconds to the first content, and the arrival time of every content character."""
    arrivals: list[float] = []
    started = time.perf_counter()
    with httpx.stream("POST", url, json=body, headers=headers, timeout=60) as response:
        for line in response.iter_lines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            chunk = json.loads(line[6:])
            content = "".join(c["delta"].get("content") or "" for c in chunk.get("choices", []))
            if content:
                arrivals.extend([time.perf_counter() - started] * len(content))
    return arrivals[0], arrivals


def latency(requests: int, env: dict[str, str]) -> dict[str, Any]:
    text = PROSE * 2
    mock_port, gateway_port = free_port(), free_port()
    mock_args = ["mock_providers", "--token-interval-ms", str(TOKEN_INTERVAL_MS), "--text", text]
    gateway_env = {**env, "PRIMARY_BASE_URL": f"http://127.0.0.1:{mock_port}/v1"}
    body = {"model": "mock", "messages": [{"role": "user", "content": "status?"}], "stream": True}
    direct_url = f"http://127.0.0.1:{mock_port}/v1/chat/completions"
    gateway_url = f"http://127.0.0.1:{gateway_port}/v1/chat/completions"
    with process(mock_args, mock_port, env), process(["llm_gateway"], gateway_port, gateway_env):
        headers: dict[str, str] = {}
        for _ in range(3):
            first_token(direct_url, body, headers)
            first_token(gateway_url, body, headers)
        direct = [first_token(direct_url, body, headers) for _ in range(requests)]
        gateway = [first_token(gateway_url, body, headers) for _ in range(requests)]

    direct_ttft = [ms(t) for t, _ in direct]
    gateway_ttft = [ms(t) for t, _ in gateway]
    baseline = [sorted(run[1][i] for run in direct)[len(direct) // 2] for i in range(len(text))]
    delays = [ms(run[1][i] - baseline[i]) for run in gateway for i in range(len(text))]
    return {
        "requests": requests,
        "token_interval_ms": TOKEN_INTERVAL_MS,
        "characters_per_reply": len(text),
        "tokens_per_reply": len(tokens(text)),
        "ttft_direct_ms": {
            "p50": percentile(direct_ttft, 0.5),
            "p99": percentile(direct_ttft, 0.99),
        },
        "ttft_gateway_ms": {
            "p50": percentile(gateway_ttft, 0.5),
            "p99": percentile(gateway_ttft, 0.99),
        },
        "added_delay_per_character_ms": {
            "p50": percentile(delays, 0.5),
            "p99": percentile(delays, 0.99),
        },
    }


def memory(megabytes: int, env: dict[str, str]) -> dict[str, Any]:
    block = PROSE * (4096 // len(PROSE) + 1)
    repeat = megabytes * 1_000_000 // len(block)
    mock_port, gateway_port = free_port(), free_port()
    mock_args = [
        "mock_providers",
        "--chunk-chars",
        "4096",
        "--repeat",
        str(repeat),
        "--text",
        block,
    ]
    gateway_env = {**env, "PRIMARY_BASE_URL": f"http://127.0.0.1:{mock_port}/v1"}
    body = {
        "model": "mock",
        "messages": [{"role": "user", "content": "everything"}],
        "stream": True,
    }
    url = f"http://127.0.0.1:{gateway_port}/v1/chat/completions"
    received = 0
    with (
        process(mock_args, mock_port, env),
        process(["llm_gateway"], gateway_port, gateway_env) as gateway,
    ):
        httpx.post(url, json={**body, "max_tokens": 4}, timeout=60)
        before = rss_mb(gateway.pid)
        started = time.perf_counter()
        with httpx.stream("POST", url, json=body, timeout=600) as response:
            for piece in response.iter_bytes():
                received += len(piece)
        elapsed = time.perf_counter() - started
        after = rss_mb(gateway.pid)
    return {
        "stream_mb": round(received / 1e6, 1),
        "seconds": round(elapsed, 1),
        "gateway_rss_mb_before": round(before, 1),
        "gateway_rss_mb_after": round(after, 1),
    }


def ms(seconds: float) -> float:
    return round(seconds * 1000, 2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--quick", action="store_true", help="the short setting CI uses")
    parser.add_argument("--out", type=Path, default=Path("results/bench.json"))
    args = parser.parse_args()
    requests, stream_mb, redactor_mb = (10, 5, 2) if args.quick else (60, 50, 20)
    with tempfile.TemporaryDirectory() as scratch:
        env = {
            **os.environ,
            "LLM_GATEWAY_SECRET": SECRET,
            "LLM_GATEWAY_DB": f"{scratch}/gateway.db",
            "LOG_LEVEL": "WARNING",
        }
        results = {
            "date": datetime.now(UTC).isoformat(timespec="seconds"),
            "machine": machine(),
            "setting": "quick" if args.quick else "full",
            "redactor": redactor_alone(redactor_mb),
            "latency": latency(requests, env),
            "memory": memory(stream_mb, env),
        }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
