from __future__ import annotations

import re
import uuid
from typing import Any

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

_TICKET_PATH = re.compile(r"/api/tickets/(\d+)(?:/.*)?$")
_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach request-scoped logging tags and echo the request id back."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        candidate = request.headers.get("X-Request-ID", "")
        request_id = candidate if _REQUEST_ID.fullmatch(candidate) else uuid.uuid4().hex
        context: dict[str, Any] = {
            "request_id": request_id,
            "component": "api",
        }
        match = _TICKET_PATH.fullmatch(request.url.path)
        if match is not None:
            context["ticket_id"] = int(match.group(1))

        with structlog.contextvars.bound_contextvars(**context):
            response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response
