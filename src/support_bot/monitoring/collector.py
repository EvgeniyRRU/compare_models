"""Gauge «открытые обращения по статусу», вычисляемый запросом к БД при скрейпе.

Каждая реплика отдаёт одинаковое значение, поэтому в Prometheus его агрегируют через
max(), а не sum().
"""

import asyncio
from collections.abc import Iterable

import structlog
from prometheus_client.core import GaugeMetricFamily, Metric
from prometheus_client.registry import Collector
from sqlalchemy import func, select

from support_bot.db.engine import SessionFactory
from support_bot.db.models import Ticket
from support_bot.domain.enums import FINAL_STATUSES, TicketStatus

log = structlog.get_logger(__name__)

OPEN_STATUSES = tuple(s for s in TicketStatus if s not in FINAL_STATUSES)
METRIC_NAME = "support_bot_open_tickets"
METRIC_HELP = "Открытые обращения по статусу (одинаково на всех репликах: агрегировать max())"


class OpenTicketsCollector(Collector):
    """Вызывается из потока HTTP-сервера метрик; запрос выполняется в event loop приложения."""

    def __init__(
        self, session_factory: SessionFactory, loop: asyncio.AbstractEventLoop, timeout: float = 5.0
    ) -> None:
        self._sf = session_factory
        self._loop = loop
        self._timeout = timeout

    def describe(self) -> Iterable[Metric]:
        # Без describe() registry вызвал бы collect() при регистрации — из потока event loop.
        return [GaugeMetricFamily(METRIC_NAME, METRIC_HELP, labels=["status"])]

    async def _query(self) -> dict[TicketStatus, int]:
        async with self._sf() as session:
            rows = await session.execute(
                select(Ticket.status, func.count())
                .where(Ticket.status.in_(OPEN_STATUSES))
                .group_by(Ticket.status)
            )
            return {status: int(count) for status, count in rows}

    def collect(self) -> Iterable[Metric]:
        gauge = GaugeMetricFamily(METRIC_NAME, METRIC_HELP, labels=["status"])
        try:
            future = asyncio.run_coroutine_threadsafe(self._query(), self._loop)
            counts = future.result(self._timeout)
        except Exception:
            log.warning("open_tickets_collect_failed", exc_info=True)
            return [gauge]
        for status in OPEN_STATUSES:
            gauge.add_metric([status.value], counts.get(status, 0))
        return [gauge]
