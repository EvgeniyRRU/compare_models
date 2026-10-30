from collections.abc import Iterable

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest
from prometheus_client.core import GaugeMetricFamily, Metric
from prometheus_client.registry import Collector
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Ticket
from app.domain.state_machine import Status

REGISTRY = CollectorRegistry(auto_describe=True)

HTTP_REQUESTS = Counter(
    "http_requests_total", "HTTP requests", ["method", "route", "status"], registry=REGISTRY
)
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds", "HTTP latency", ["method", "route"], registry=REGISTRY
)
TICKETS_CREATED = Counter(
    "tickets_created_total", "Created tickets", ["priority"], registry=REGISTRY
)
TICKET_TRANSITIONS = Counter(
    "ticket_transitions_total",
    "Ticket status transitions",
    ["action", "to_status"],
    registry=REGISTRY,
)
TIME_TO_TAKE = Histogram(
    "ticket_time_to_take_seconds",
    "Time from last entry into 'new' to take",
    buckets=(60, 300, 600, 1800, 3600, 7200, 14400, 28800, 86400, 172800),
    registry=REGISTRY,
)
UPDATES = Counter("telegram_updates_total", "Telegram updates", ["result"], registry=REGISTRY)
REMINDERS_SENT = Counter("reminders_sent_total", "Reminders sent", ["priority"], registry=REGISTRY)
ERRORS = Counter("errors_total", "Errors", ["component"], registry=REGISTRY)


class OpenTicketsCollector(Collector):
    """Gauge открытых обращений: значение берётся из БД при скрейпе.

    Каждая реплика отдаёт одно и то же, поэтому агрегировать нужно max(), а не sum().
    """

    def __init__(self) -> None:
        self._values: dict[str, int] = {}

    async def refresh(self, sm: async_sessionmaker[AsyncSession]) -> None:
        async with sm() as s:
            rows = (
                await s.execute(select(Ticket.status, func.count()).group_by(Ticket.status))
            ).all()
        counts = dict(rows)
        self._values = {st.value: counts.get(st.value, 0) for st in Status}

    def collect(self) -> Iterable[Metric]:
        g = GaugeMetricFamily(
            "tickets_open", "Tickets by status (aggregate with max())", labels=["status"]
        )
        for status, value in self._values.items():
            g.add_metric([status], value)
        yield g


open_tickets = OpenTicketsCollector()
REGISTRY.register(open_tickets)


def render() -> bytes:
    return generate_latest(REGISTRY)
