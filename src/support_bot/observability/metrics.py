"""One in-process registry. Database gauges are queried anew at each scrape."""

import asyncio
import time
from collections.abc import Iterable
from typing import TYPE_CHECKING

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from prometheus_client.core import GaugeMetricFamily
from sqlalchemy import func, select
from starlette.types import ASGIApp, Message, Receive, Scope, Send

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class OpenTicketsCollector:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], loop: asyncio.AbstractEventLoop):
        self.sessions = sessions
        self.loop = loop

    async def counts(self) -> dict[str, int]:
        from support_bot.db.models import Ticket

        async with self.sessions() as session:
            rows = await session.execute(
                select(Ticket.status, func.count())
                .where(Ticket.status.in_(["new", "in_progress", "waiting_info"]))
                .group_by(Ticket.status)
            )
            return {str(status): int(count) for status, count in rows}

    def describe(self) -> Iterable[GaugeMetricFamily]:
        yield GaugeMetricFamily(
            "support_open_tickets", "Open tickets (aggregate using max)", labels=["status"]
        )

    def collect(self) -> Iterable[GaugeMetricFamily]:
        # prometheus_client's HTTP server scrapes from a separate thread. Execute
        # the query on the application's event loop, where its async pool lives.
        future = asyncio.run_coroutine_threadsafe(self.counts(), self.loop)
        try:
            counts = future.result(timeout=5)
        except BaseException:
            future.cancel()
            raise
        gauge = GaugeMetricFamily(
            "support_open_tickets", "Open tickets (aggregate using max)", labels=["status"]
        )
        for status in ("new", "in_progress", "waiting_info"):
            gauge.add_metric([status], counts.get(status, 0))
        yield gauge


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.http_requests = Counter(
            "support_http_requests_total",
            "HTTP responses",
            ["method", "route", "status"],
            registry=self.registry,
        )
        self.http_latency = Histogram(
            "support_http_request_seconds",
            "HTTP latency",
            ["method", "route"],
            registry=self.registry,
        )
        self.tickets_created = Counter(
            "support_tickets_created_total", "Created tickets", ["priority"], registry=self.registry
        )
        self.transitions = Counter(
            "support_ticket_transitions_total",
            "Status transitions",
            ["old_status", "new_status"],
            registry=self.registry,
        )
        self.time_to_take = Histogram(
            "support_ticket_time_to_take_seconds",
            "Seconds since latest entry into new",
            buckets=(30, 60, 300, 600, 1800, 3600, 7200, 28800, 86400),
            registry=self.registry,
        )
        self.updates = Counter(
            "support_updates_total", "Telegram updates", ["result"], registry=self.registry
        )
        self.reminders = Counter(
            "support_reminders_sent_total", "Successfully sent reminders", registry=self.registry
        )
        self.errors = Counter(
            "support_errors_total", "Errors", ["component"], registry=self.registry
        )


metrics = Metrics()


class HTTPMetricsMiddleware:
    def __init__(self, app: ASGIApp, metrics: Metrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500

        async def observed_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, observed_send)
        finally:
            route = getattr(scope.get("route"), "path", "unmatched")
            method = (
                scope["method"]
                if scope["method"] in {"GET", "POST", "PATCH", "PUT", "DELETE", "HEAD", "OPTIONS"}
                else "OTHER"
            )
            self.metrics.http_requests.labels(method, route, str(status)).inc()
            self.metrics.http_latency.labels(method, route).observe(time.perf_counter() - started)


async def render_metrics(registry: CollectorRegistry) -> tuple[bytes, str]:
    """Useful for tests; collectors must run off the database's event loop."""
    return await asyncio.to_thread(generate_latest, registry), CONTENT_TYPE_LATEST
