"""Prometheus business metrics. Registry is per-process; pods sum via sum() except
the open-tickets gauge, which must be aggregated with max() (every replica reports
the same DB-derived value)."""

import time
from collections.abc import Mapping
from typing import Any

from prometheus_client import Counter, Gauge, Histogram, generate_latest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.db.models import FINAL_STATUSES, Ticket, TicketStatus
from app.logging_setup import get_logger

log = get_logger("metrics")

TICKETS_CREATED = Counter("tickets_created_total", "Created tickets", ["priority"])
TICKET_TRANSITIONS = Counter(
    "ticket_transitions_total", "Ticket status transitions", ["from_status", "to_status"]
)
TICKET_TIME_TO_TAKE = Histogram(
    "ticket_time_to_take_seconds",
    "Time from entering status new to being taken",
    ["priority"],
    buckets=(60, 300, 600, 1800, 3600, 7200, 28800, 86400),
)
UPDATES_PROCESSED = Counter("updates_processed_total", "Telegram updates processed")
UPDATES_DEDUPLICATED = Counter(
    "updates_deduplicated_total", "Telegram updates skipped as duplicates"
)
REMINDERS_SENT = Counter("reminders_sent_total", "Reminder messages sent", ["priority"])
POST_SYNC_TOTAL = Counter("post_sync_total", "Post sync outcomes", ["result"])
APP_ERRORS = Counter("app_errors_total", "Handled errors", ["component"])

# --- HTTP layer metrics (labels use the ROUTE TEMPLATE, never raw ids) -------
HTTP_REQUESTS = Counter(
    "http_requests_total", "HTTP requests served", ["method", "route", "status"]
)
HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds", "HTTP request latency", ["method", "route"]
)
OPEN_TICKETS_BY_STATUS = Gauge(
    "open_tickets_by_status",
    "Tickets in active statuses (same on every replica; aggregate with max)",
    ["status"],
)


def _route_label(scope: Mapping[str, Any]) -> str:
    route = scope.get("route")
    path = getattr(route, "path_template", None) or getattr(route, "path", None)
    return str(path) if path else "unmatched"


class MetricsMiddleware:
    """Pure-ASGI middleware: counts requests and observes latency per route template."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        start = time.perf_counter()
        status = 0

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - start
            route = _route_label(scope)
            method = str(scope.get("method", ""))
            HTTP_REQUESTS.labels(method, route, str(status or 500)).inc()
            HTTP_REQUEST_DURATION.labels(method, route).observe(elapsed)


async def refresh_open_tickets(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Scrape-time gauge: query DB and publish counts for every open status.

    Open = everything that is not final, i.e. new counts too (ACTIVE_STATUSES is
    the assignee-scoped set and deliberately excludes 'new').
    """
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(Ticket.status, func.count(Ticket.id))
                .where(Ticket.status.notin_(FINAL_STATUSES))
                .group_by(Ticket.status)
            )
        ).all()
    counts: dict[str, int] = {status.value: 0 for status in TicketStatus}
    for status, total in rows:
        counts[status.value] = int(total)
    for status_value, total in counts.items():
        OPEN_TICKETS_BY_STATUS.labels(status_value).set(total)


def create_metrics_app(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> Starlette:
    """Standalone /metrics server app (separate port, never exposed via ingress)."""

    async def metrics_endpoint(request: Request) -> Response:
        if session_factory is not None:
            try:
                await refresh_open_tickets(session_factory)
            except Exception as exc:  # scrape must not die on a DB hiccup
                log.warning("metrics_gauge_refresh_failed", error=str(exc))
        return PlainTextResponse(
            generate_latest().decode("utf-8"),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    async def health(_request: Request) -> Response:
        return PlainTextResponse("ok")

    return Starlette(
        routes=[
            Route("/metrics", metrics_endpoint, methods=["GET"]),
            Route("/healthz", health, methods=["GET"]),
        ]
    )
