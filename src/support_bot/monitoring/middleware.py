"""ASGI-middleware: request_id в contextvars, HTTP-метрики и access-лог.

В label маршрута пишется шаблон пути (например, /api/tickets/{ticket_id}), а не URL.
"""

import time
import uuid

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from support_bot.monitoring import metrics

log = structlog.get_logger("support_bot.http")

_SKIP_ACCESS_LOG = frozenset({"/health/live", "/health/ready"})


def route_template(scope: Scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "unmatched"


class ObservabilityMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        incoming = headers.get(b"x-request-id", b"").decode("latin-1")[:64]
        request_id = incoming or uuid.uuid4().hex
        status_code = 500
        started = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                message.setdefault("headers", [])
                message["headers"].append((b"x-request-id", request_id.encode("latin-1")))
            await send(message)

        with structlog.contextvars.bound_contextvars(request_id=request_id, component="http"):
            try:
                await self.app(scope, receive, send_wrapper)
            finally:
                elapsed = time.perf_counter() - started
                route = route_template(scope)
                method = scope.get("method", "")
                metrics.HTTP_REQUESTS.labels(
                    method=method, route=route, status=str(status_code)
                ).inc()
                metrics.HTTP_LATENCY.labels(method=method, route=route).observe(elapsed)
                if route not in _SKIP_ACCESS_LOG:
                    log.info(
                        "http_request",
                        method=method,
                        route=route,
                        status=status_code,
                        duration_ms=round(elapsed * 1000, 1),
                    )
