"""A scripted session against a fresh server process, with a pass summary on stderr."""

from __future__ import annotations

import sys

import anyio
from mcp import Client, MCPError, StdioServerParameters
from mcp.types import INVALID_PARAMS

from customer_server.samples import REJECTED, VALID_REFUND, Rejected


async def _rejected_with_invalid_params(client: Client, case: Rejected) -> bool:
    try:
        await client.call_tool(case.tool, case.arguments)
    except MCPError as exc:
        errors = exc.data["errors"] if isinstance(exc.data, dict) else []
        return exc.code == INVALID_PARAMS and any(
            e["field"] == case.field and e["rule"] == case.rule for e in errors
        )
    return False


async def _session() -> list[tuple[str, bool]]:
    checks: list[tuple[str, bool]] = []
    server = StdioServerParameters(
        command=sys.executable, args=["-m", "customer_server"], env={"LOG_LEVEL": "WARNING"}
    )
    async with Client(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
        checks.append(("lists both tools", listed == {"get_customer_record", "trigger_refund"}))

        record = await client.call_tool("get_customer_record", {"customer_id": "CUST-10001"})
        checks.append(("reads a customer record", not record.is_error))

        first = await client.call_tool("trigger_refund", VALID_REFUND)
        again = await client.call_tool("trigger_refund", VALID_REFUND)
        replayed = (
            not first.is_error
            and again.structured_content["refund_id"] == first.structured_content["refund_id"]
            and again.structured_content["replayed"] is True
        )
        checks.append(("a repeated refund returns the original refund id", replayed))

        unknown = await client.call_tool("get_customer_record", {"customer_id": "CUST-99999"})
        checks.append(("an unknown customer is a tool error", unknown.is_error))

        empty = await client.call_tool(
            "trigger_refund", {**VALID_REFUND, "customer_id": "CUST-10004"}
        )
        checks.append(("a refund over the balance is a tool error", empty.is_error))

        for case in REJECTED:
            ok = await _rejected_with_invalid_params(client, case)
            checks.append((f"rejects {case.name} with -32602", ok))
    return checks


def run_self_test() -> int:
    """Run the session and return a process exit code."""
    checks = anyio.run(_session)
    for name, ok in checks:
        print(f"{'pass' if ok else 'FAIL'}  {name}", file=sys.stderr)
    passed = sum(ok for _, ok in checks)
    print(f"self test: {passed} of {len(checks)} checks passed", file=sys.stderr)
    return 0 if passed == len(checks) else 1
