"""Safe, uniform error responses.

Every error leaving the gateway has the same envelope::

    {"error": {"code": "...", "message": "...", "traceId": "..."}}

Messages are fixed, human-readable strings chosen here — never exception
text, stack traces, paths, hostnames or dependency internals.  ``traceId`` is
a per-request random id safe to show in the client UI and to quote to the
operator; it carries no request content.
"""

from __future__ import annotations

import secrets

from fastapi.responses import JSONResponse

_MESSAGES = {
    "unauthorized": "Authentication required.",
    "forbidden": "This credential does not allow that.",
    "not_found": "Resource not found.",
    "resync_required": "The cursor is no longer valid; refresh the snapshot.",
    "client_version_required": "Send X-AICC-Client-Version.",
    "unsupported_client_version": "This client version is not supported.",
    "unsupported_accept": "Only application/json responses are available.",
    "validation_failed": "The request is not valid.",
    "rate_limited": "Too many requests; slow down.",
    "internal": "Internal error.",
}


def new_trace_id() -> str:
    return secrets.token_hex(8)


class GatewayError(Exception):
    """A deliberate, safe-to-serialize API error."""

    def __init__(
        self, status_code: int, code: str, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.headers = headers or {}


def error_response(
    status_code: int,
    code: str,
    trace_id: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = {
        "error": {
            "code": code,
            "message": _MESSAGES.get(code, _MESSAGES["internal"]),
            "traceId": trace_id or new_trace_id(),
        }
    }
    return JSONResponse(status_code=status_code, content=body, headers=headers)
