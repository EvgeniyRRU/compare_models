from __future__ import annotations

import time

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from support_bot.db.enums import OPEN_STATUSES, TicketPriority, TicketStatus
from support_bot.db.models.ticket import Ticket

TICKETS_CREATED = Counter(
    "support_bot_tickets_created_total",
    "Tickets created, by priority",
    ["priority"],
)
TICKET_TRANSITIONS = Counter(
    "support_bot_ticket_transitions_total",
    "Ticket status transitions",
    ["from_status", "to_status"],
)
TICKET_TIME_TO_TAKE = Histogram(
    "support_bot_ticket_time_to_take_seconds",
    "Seconds between entering 'new' and being taken",
    buckets=(60, 300, 900, 1800, 3600, 7200, 14400, 28800, 86400, 172800),
)
REMINDERS_SENT = Counter(
    "support_bot_reminders_sent_total",
    "Reminders delivered to the developers chat",
    ["priority"],
)
UPDATES_PROCESSED = Counter(
    "support_bot_updates_processed_total",
    "Telegram updates accepted for processing",
)
UPDATES_DUPLICATE = Counter(
    "support_bot_updates_duplicate_total",
    "Telegram updates skipped as duplicates",
)
ERRORS = Counter(
    "support_bot_errors_total",
    "Handled errors, by component",
    ["component"],
)
HTTP_REQUESTS = Counter(
    "support_bot_http_requests_total",
    "HTTP requests, by method, route template and status",
    ["method", "route", "status"],
)
HTTP_DURATION = Histogram(
    "support_bot_http_request_duration_seconds",
    "HTTP request duration, by method and route template",
    ["method", "route"],
)
OPEN_TICKETS = Gauge(
    "support_bot_open_tickets",
    "Open tickets by status (scrape-time DB value; aggregate with max())",
    ["status"],
)


def observe_ticket_created(priority: TicketPriority) -> None:
    TICKETS_CREATED.labels(priority=priority.value).inc()


def observe_transition(old: TicketStatus, new: TicketStatus) -> None:
    TICKET_TRANSITIONS.labels(from_status=old.value, to_status=new.value).inc()


def observe_time_to_take(seconds: float) -> None:
    TICKET_TIME_TO_TAKE.observe(max(seconds, 0.0))


def observe_reminder_sent(priority: TicketPriority) -> None:
    REMINDERS_SENT.labels(priority=priority.value).inc()


def observe_error(component: str) -> None:
    ERRORS.labels(component=component).inc()


def observe_update(duplicate: bool) -> None:
    if duplicate:
        UPDATES_DUPLICATE.inc()
    else:
        UPDATES_PROCESSED.inc()


async def refresh_open_tickets(session: AsyncSession) -> dict[TicketStatus, int]:
    """Run at scrape time; returns open ticket counts by status."""
    rows = await session.execute(
        select(Ticket.status, func.count())
        .where(Ticket.status.in_(OPEN_STATUSES))
        .group_by(Ticket.status)
    )
    counts: dict[TicketStatus, int] = {status: 0 for status in OPEN_STATUSES}
    for status, count in rows:
        counts[TicketStatus(status)] = int(count)
    for status, count in counts.items():
        OPEN_TICKETS.labels(status=status.value).set(count)
    return counts


def render_metrics() -> bytes:
    return generate_latest()


METRICS_CONTENT_TYPE = CONTENT_TYPE_LATEST


class PrometheusMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            route = request.scope.get("route")
            route_path = getattr(route, "path", None) or "__unmatched__"
            HTTP_REQUESTS.labels(
                method=request.method, route=route_path, status=str(status_code)
            ).inc()
            HTTP_DURATION.labels(method=request.method, route=route_path).observe(
                time.perf_counter() - started
            )
