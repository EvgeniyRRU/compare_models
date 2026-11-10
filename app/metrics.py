import asyncio
import logging

from prometheus_client import Counter, Histogram
from prometheus_client.core import GaugeMetricFamily
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app.db.models import Ticket

CREATED = Counter("tickets_created_total", "Tickets created", ["priority"])
TRANSITIONS = Counter(
    "ticket_transitions_total", "Ticket transitions", ["from_status", "to_status"]
)
TAKE_TIME = Histogram("ticket_time_to_take_seconds", "Time from entering new status until taking")
ERRORS = Counter("application_errors_total", "Application errors", ["component"])


def record_after_commit(session, metric, *labels_or_value):
    session.sync_session.info.setdefault("ticket_metrics", []).append((metric, labels_or_value))


@event.listens_for(Session, "after_commit")
def _publish_metrics(session):
    for metric, values in session.info.pop("ticket_metrics", []):
        if metric == "created":
            CREATED.labels(*values).inc()
        elif metric == "transition":
            TRANSITIONS.labels(*values).inc()
        elif metric == "take_time":
            TAKE_TIME.observe(values[0])


@event.listens_for(Session, "after_rollback")
def _discard_metrics(session):
    session.info.pop("ticket_metrics", None)


class OpenTicketsCollector:
    def __init__(self, sessions, loop):
        self.sessions = sessions
        self.loop = loop

    async def _fetch(self):
        async with self.sessions() as session:
            return (
                await session.execute(
                    select(Ticket.status, func.count())
                    .where(Ticket.status.in_(["new", "in_progress", "waiting_info"]))
                    .group_by(Ticket.status)
                )
            ).all()

    def describe(self):
        yield GaugeMetricFamily(
            "tickets_open", "Open tickets by status; aggregate pods using max", labels=["status"]
        )

    def collect(self):
        gauge = GaugeMetricFamily(
            "tickets_open", "Open tickets by status; aggregate pods using max", labels=["status"]
        )
        future = asyncio.run_coroutine_threadsafe(self._fetch(), self.loop)
        try:
            counts = dict(future.result(timeout=5))
            for status in ("new", "in_progress", "waiting_info"):
                gauge.add_metric([status], counts.get(status, 0))
            yield gauge
        except Exception:
            future.cancel()
            logging.getLogger(__name__).exception("Unable to collect open tickets")
