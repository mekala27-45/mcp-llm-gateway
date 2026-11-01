"""One error shape for every failure a client can see."""

from __future__ import annotations

import json

from starlette.responses import JSONResponse

STATUS = {
    "invalid_request": 400,
    "unauthorized": 401,
    "request_too_large": 413,
    "rate_limited": 429,
    "internal_error": 500,
    "upstream_unavailable": 503,
}


class GatewayError(Exception):
    """A failure with a message written for the client. Nothing upstream goes in it."""

    def __init__(self, kind: str, code: str, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        if kind not in STATUS:
            raise ValueError(f"unknown error type {kind}")
        self.kind = kind
        self.code = code
        self.message = message
        self.retry_after = retry_after

    @property
    def status(self) -> int:
        return STATUS[self.kind]

    def body(self, request_id: str) -> dict[str, dict[str, str]]:
        return {
            "error": {
                "type": self.kind,
                "code": self.code,
                "message": self.message,
                "request_id": request_id,
            }
        }


def internal_error() -> GatewayError:
    return GatewayError(
        "internal_error", "internal_error", "Something failed on our side. Try again."
    )


def upstream_unavailable() -> GatewayError:
    return GatewayError(
        "upstream_unavailable",
        "no_provider_available",
        "No provider could serve this request. Try again.",
    )


def error_response(error: GatewayError, request_id: str) -> JSONResponse:
    headers = {"x-request-id": request_id}
    if error.retry_after is not None:
        headers["retry-after"] = str(error.retry_after)
    if error.kind == "unauthorized":
        headers["www-authenticate"] = 'Bearer realm="llm-gateway"'
    return JSONResponse(error.body(request_id), status_code=error.status, headers=headers)


def error_event(error: GatewayError, request_id: str) -> bytes:
    """The same error as a last SSE event, for a stream that has already started."""
    return f"data: {json.dumps(error.body(request_id))}\n\n".encode()
