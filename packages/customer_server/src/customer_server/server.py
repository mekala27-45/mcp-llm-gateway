"""The customer MCP server: two tools on the SDK low level Server, served over stdio."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any, Literal

import anyio
from mcp import MCPError
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.types import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)
from pydantic import BaseModel, ValidationError

from customer_server.ledger import Ledger, RefundRejected, UnknownCustomer
from customer_server.models import FieldError, GetCustomerRecord, TriggerRefund, field_errors
from gateway_common.logs import configure, log_event, request_id

ValidationMode = Literal["protocol", "tool"]
ToolHandler = Callable[[dict[str, Any]], Awaitable[CallToolResult]]

logger = logging.getLogger("customer_server")

_NO_ARGUMENTS: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}


class _ArgumentsRejected(Exception):
    def __init__(self, errors: list[FieldError]) -> None:
        super().__init__("arguments rejected")
        self.errors = errors


def _parse[ArgumentsT: BaseModel](model: type[ArgumentsT], arguments: dict[str, Any]) -> ArgumentsT:
    try:
        return model.model_validate(arguments)
    except ValidationError as exc:
        raise _ArgumentsRejected(field_errors(exc)) from None


def _ok(payload: dict[str, Any]) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload))], structured_content=payload
    )


def _refused(message: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=message)], is_error=True)


def _arguments_result(errors: list[FieldError]) -> CallToolResult:
    text = "The arguments were not accepted: " + "; ".join(e["message"] for e in errors) + "."
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content={"errors": errors},
        is_error=True,
    )


class CustomerTools:
    """Lists the tools and runs a call, mapping every outcome to the right kind of error."""

    def __init__(self, ledger: Ledger, mode: ValidationMode, test_tools: bool) -> None:
        self._ledger = ledger
        self._mode = mode
        self._handlers: dict[str, ToolHandler] = {
            "get_customer_record": self._get_customer_record,
            "trigger_refund": self._trigger_refund,
        }
        self._tools = [
            Tool(
                name="get_customer_record",
                description="Look up one customer by id: name, email, tier and refundable balance.",
                input_schema=GetCustomerRecord.model_json_schema(),
            ),
            Tool(
                name="trigger_refund",
                description=(
                    "Refund an amount from the refundable balance of a customer. Asking for the "
                    "same refund again within 60 seconds returns the original refund id."
                ),
                input_schema=TriggerRefund.model_json_schema(),
            ),
        ]
        if test_tools:
            self._handlers["debug_stdout_noise"] = self._stdout_noise
            self._handlers["debug_crash"] = self._crash
            self._tools += [
                Tool(
                    name="debug_stdout_noise",
                    description="Test only: writes to descriptor 1 three different ways.",
                    input_schema=_NO_ARGUMENTS,
                ),
                Tool(
                    name="debug_crash",
                    description="Test only: raises an unexpected exception.",
                    input_schema=_NO_ARGUMENTS,
                ),
            ]

    async def list_tools(
        self, ctx: ServerRequestContext[Any], params: PaginatedRequestParams | None
    ) -> ListToolsResult:
        return ListToolsResult(tools=self._tools)

    async def call_tool(
        self, ctx: ServerRequestContext[Any], params: CallToolRequestParams
    ) -> CallToolResult:
        token = request_id.set(str(ctx.request_id))
        started = time.perf_counter()
        outcome = "ok"
        try:
            handler = self._handlers.get(params.name)
            if handler is None:
                outcome = "unknown_tool"
                raise MCPError(INVALID_PARAMS, "Unknown tool")
            try:
                result = await handler(params.arguments or {})
            except _ArgumentsRejected as rejected:
                outcome = "invalid_arguments"
                if self._mode == "tool":
                    return _arguments_result(rejected.errors)
                raise MCPError(
                    INVALID_PARAMS,
                    "Invalid params",
                    {"tool": params.name, "errors": rejected.errors},
                ) from None
            if result.is_error:
                outcome = "refused"
            return result
        except MCPError:
            raise
        except Exception:
            outcome = "internal_error"
            logger.exception("tool call failed")
            raise MCPError(INTERNAL_ERROR, "Internal error") from None
        finally:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            log_event(
                logger, "tool call", tool=params.name[:64], outcome=outcome, duration_ms=elapsed_ms
            )
            request_id.reset(token)

    async def _get_customer_record(self, arguments: dict[str, Any]) -> CallToolResult:
        args = _parse(GetCustomerRecord, arguments)
        customer = self._ledger.customer(args.customer_id)
        if customer is None:
            return _refused("No customer has that customer_id.")
        return _ok(
            {
                "customer_id": customer.customer_id,
                "name": customer.name,
                "email": customer.email,
                "tier": customer.tier,
                "customer_since": customer.customer_since,
                "refundable_balance": f"{self._ledger.refundable(customer.customer_id):.2f}",
                "currency": "USD",
            }
        )

    async def _trigger_refund(self, arguments: dict[str, Any]) -> CallToolResult:
        args = _parse(TriggerRefund, arguments)
        amount = Decimal(repr(args.amount)).quantize(Decimal("0.01"))
        try:
            refund, replayed = self._ledger.refund(args.customer_id, amount, args.reason)
        except UnknownCustomer:
            return _refused("No customer has that customer_id.")
        except RefundRejected as exc:
            return _refused(str(exc))
        return _ok(
            {
                "refund_id": refund.refund_id,
                "customer_id": refund.customer_id,
                "amount": f"{refund.amount:.2f}",
                "currency": "USD",
                "status": "accepted",
                "replayed": replayed,
                "refundable_balance": f"{self._ledger.refundable(refund.customer_id):.2f}",
            }
        )

    async def _stdout_noise(self, arguments: dict[str, Any]) -> CallToolResult:
        print("stray print from a tool handler")
        os.write(1, b"stray os.write to descriptor 1\n")
        await anyio.run_process(
            [sys.executable, "-c", "print('stray print from a child process')"],
            stdout=None,
            stderr=None,
        )
        return _ok({"writes": 3})

    async def _crash(self, arguments: dict[str, Any]) -> CallToolResult:
        raise RuntimeError("ledger unavailable at /srv/customer_server/ledger.py")


def build_server(
    ledger: Ledger | None = None, mode: ValidationMode = "protocol", test_tools: bool = False
) -> Server[Any]:
    """Build the server. Tests use it in process; serve() runs it on stdio."""
    tools = CustomerTools(ledger or Ledger(), mode, test_tools)
    return Server(
        "customer-server",
        version="1.0.0",
        instructions="Customer lookups and refunds against a mock ledger.",
        on_list_tools=tools.list_tools,
        on_call_tool=tools.call_tool,
    )


def serve() -> None:
    """Serve on stdin and stdout until the client closes its end."""
    configure(os.environ.get("LOG_LEVEL", "INFO"))
    raw_mode = os.environ.get("VALIDATION_ERRORS", "protocol")
    if raw_mode not in ("protocol", "tool"):
        logger.error("VALIDATION_ERRORS must be protocol or tool")
        raise SystemExit(2)
    mode: ValidationMode = "tool" if raw_mode == "tool" else "protocol"
    test_tools = os.environ.get("CUSTOMER_SERVER_TEST_TOOLS") == "1"
    server = build_server(Ledger(), mode, test_tools)
    log_event(logger, "serving on stdio", validation_errors=mode, test_tools=test_tools)
    anyio.run(_serve, server)


async def _serve(server: Server[Any]) -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())
