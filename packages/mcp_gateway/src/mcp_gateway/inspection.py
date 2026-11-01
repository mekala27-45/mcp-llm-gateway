"""Reads a request body strictly and decides from the body alone.

No header the client controls decides anything here. The routing headers are only
compared with the body, and a request whose headers disagree with it is refused.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from mcp.shared.inbound import (
    NAME_BEARING_METHODS,
    decode_header_value,
    find_duplicated_routing_header,
)
from mcp.types import (
    HEADER_MISMATCH,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    PROTOCOL_VERSION_META_KEY,
)

# -32000 to -32019 is the implementation defined band in the 2026-07-28 error code
# policy, so the code the task asks for does not collide with a spec code. The Python
# SDK also uses -32001 internally for a request timeout that never goes on the wire,
# so a client should read the message as well as the code.
UNAUTHORIZED_TOOL_CALL = -32001

REQUEST_METHODS = frozenset({"server/discover", "tools/list", "tools/call"})
NOTIFICATION_METHODS = frozenset({"notifications/cancelled"})
ADMIN_PREFIX = "admin_"

# SEP-986 tool names: ASCII letters, digits, underscore, dash and dot.
_TOOL_NAME = re.compile(r"[A-Za-z0-9._-]{1,128}")

RequestId = str | int


class Refusal(Exception):
    """A request the gateway answers itself with a JSON-RPC error."""

    def __init__(
        self,
        code: int,
        message: str,
        request_id: RequestId | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.request_id = request_id
        self.data = data


@dataclass(frozen=True)
class RpcMessage:
    body: dict[str, Any]
    request_id: RequestId | None
    method: str
    params: dict[str, Any]

    @property
    def is_notification(self) -> bool:
        return "id" not in self.body


class _DuplicateKey(Exception):
    pass


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    # Two "name" keys are refused outright: the gateway would read one and the
    # downstream parser might keep the other.
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _no_constants(token: str) -> None:
    raise ValueError(f"{token} is not JSON")


def parse_message(raw: bytes) -> RpcMessage:
    """Parse one JSON-RPC request or notification, or refuse the body."""
    try:
        decoded = json.loads(
            raw, object_pairs_hook=_object_without_duplicates, parse_constant=_no_constants
        )
    except _DuplicateKey:
        raise Refusal(INVALID_REQUEST, "Duplicate keys in a JSON object are not accepted") from None
    except (ValueError, RecursionError):
        raise Refusal(PARSE_ERROR, "Parse error") from None
    if isinstance(decoded, list):
        raise Refusal(INVALID_REQUEST, "Batch requests are not accepted")
    if not isinstance(decoded, dict):
        raise Refusal(INVALID_REQUEST, "The body must be one JSON-RPC request object")

    request_id = decoded.get("id")
    valid_id = isinstance(request_id, str) or (
        isinstance(request_id, int) and not isinstance(request_id, bool)
    )
    if "id" in decoded and not valid_id:
        raise Refusal(INVALID_REQUEST, "id must be a string or an integer")
    reply_id = request_id if valid_id else None
    if decoded.get("jsonrpc") != "2.0" or "result" in decoded or "error" in decoded:
        raise Refusal(INVALID_REQUEST, "The body must be a JSON-RPC 2.0 request", reply_id)
    method = decoded.get("method")
    if not isinstance(method, str):
        raise Refusal(INVALID_REQUEST, "method must be a string", reply_id)
    params = decoded.get("params", {})
    if not isinstance(params, dict):
        raise Refusal(INVALID_PARAMS, "params must be an object", reply_id)
    return RpcMessage(body=decoded, request_id=reply_id, method=method, params=params)


def check_routing_headers(raw_headers: list[tuple[str, str]], message: RpcMessage) -> None:
    """Refuse a request whose routing headers are repeated or disagree with its body."""
    repeated = find_duplicated_routing_header(raw_headers)
    if repeated is not None:
        raise Refusal(
            HEADER_MISMATCH, f"The {repeated} header appears more than once", message.request_id
        )
    headers = {name.lower(): value for name, value in raw_headers}
    method_header = headers.get("mcp-method")
    if method_header is not None and method_header != message.method:
        raise Refusal(
            HEADER_MISMATCH, "Mcp-Method does not match the request body", message.request_id
        )
    name_key = NAME_BEARING_METHODS.get(message.method)
    name_header = headers.get("mcp-name")
    if (
        name_key is not None
        and name_header is not None
        and (decode_header_value(name_header) != message.params.get(name_key))
    ):
        raise Refusal(
            HEADER_MISMATCH, "Mcp-Name does not match the request body", message.request_id
        )
    meta = message.params.get("_meta")
    body_version = meta.get(PROTOCOL_VERSION_META_KEY) if isinstance(meta, dict) else None
    version_header = headers.get("mcp-protocol-version")
    if version_header is not None and body_version is not None and version_header != body_version:
        raise Refusal(
            HEADER_MISMATCH,
            "MCP-Protocol-Version does not match the request body",
            message.request_id,
        )


def check_method(message: RpcMessage) -> None:
    """Only the methods this gateway knows go through. Method names are compared exactly."""
    known = NOTIFICATION_METHODS if message.is_notification else REQUEST_METHODS
    if message.method not in known:
        raise Refusal(METHOD_NOT_FOUND, "Method not found", message.request_id)


def normalize_tool_name(name: str) -> str:
    """The form the admin check reads: NFKC, case folded, no format characters, no outer spaces."""
    folded = unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", name).casefold())
    # Category Cf is the zero width spaces and joiners, the byte order mark and the bidi controls.
    visible = "".join(ch for ch in folded if unicodedata.category(ch) != "Cf")
    return visible.strip()


def check_tool_call(message: RpcMessage, role: str, listed: frozenset[str] | None) -> str:
    """Return the tool name if this role may call it, or refuse the call."""
    name = message.params.get("name")
    if not isinstance(name, str):
        raise Refusal(INVALID_PARAMS, "params.name must be a string", message.request_id)
    normalized = normalize_tool_name(name)
    if normalized.startswith(ADMIN_PREFIX) and role != "admin":
        raise Refusal(
            UNAUTHORIZED_TOOL_CALL,
            "Unauthorized Tool Call",
            message.request_id,
            {"tool": normalized, "role": role},
        )
    # NFKC does not turn a Cyrillic a into a Latin a, so a look-alike name is
    # stopped here instead: only SEP-986 names are forwarded.
    if not _TOOL_NAME.fullmatch(name):
        raise Refusal(
            INVALID_PARAMS,
            "Tool names may use only letters, digits, underscore, dash and dot",
            message.request_id,
        )
    if listed is not None and name not in listed:
        raise Refusal(
            UNAUTHORIZED_TOOL_CALL,
            "Unauthorized Tool Call",
            message.request_id,
            {"tool": name, "role": role, "reason": "not listed by the server"},
        )
    return name
