"""Напоминания о невзятых обращениях: не более одной попытки отправки на каждый срок.

Расписание хранится в БД (next_reminder_at), поэтому SKIP LOCKED исключает двойную
обработку одного срока несколькими репликами.
"""

from dataclasses import dataclass

import structlog
from sqlalchemy import func, select, update

from support_bot.db.engine import SessionFactory
from support_bot.db.models import Ticket
from support_bot.domain.enums import PRIORITY_LABELS, Priority, TicketStatus
from support_bot.monitoring import metrics
from support_bot.monitoring.sentry import capture_exception
from support_bot.rendering import render_reminder
from support_bot.services.ports import PostPublisher
from support_bot.services.tickets import TicketService

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class DueReminder:
    ticket_id: int
    priority: Priority
    message_id: int | None


class ReminderService:
    def __init__(
        self,
        session_factory: SessionFactory,
        tickets: TicketService,
        publisher: PostPublisher,
        *,
        batch_size: int = 100,
    ) -> None:
        self._sf = session_factory
        self._tickets = tickets
        self._publisher = publisher
        self._batch_size = batch_size

    async def claim_due(self) -> list[DueReminder]:
        """Выбрать сработавшие сроки и сдвинуть их одной транзакцией (до отправки)."""
        async with self._sf.begin() as session:
            rows = (
                await session.execute(
                    select(Ticket.id, Ticket.priority, Ticket.message_id)
                    .where(
                        Ticket.status == TicketStatus.NEW,
                        Ticket.next_reminder_at <= func.now(),
                    )
                    .order_by(Ticket.next_reminder_at)
                    .limit(self._batch_size)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            due = [DueReminder(r.id, r.priority, r.message_id) for r in rows]
            for item in due:
                # Следующий срок — от текущего времени, без догоняющих серий. version не меняется.
                await session.execute(
                    update(Ticket)
                    .where(Ticket.id == item.ticket_id)
                    .values(next_reminder_at=self._tickets.reminder_due(item.priority))
                )
        return due

    async def run_once(self) -> int:
        due = await self.claim_due()
        sent = 0
        for item in due:
            with structlog.contextvars.bound_contextvars(ticket_id=item.ticket_id):
                text = render_reminder(item.ticket_id, PRIORITY_LABELS[item.priority])
                try:
                    await self._publisher.send_reminder(text, item.message_id)
                except Exception as exc:
                    # Повторной отправки нет: следующее напоминание придёт через интервал.
                    log.exception("reminder_send_failed")
                    metrics.REMINDERS.labels(result="failed").inc()
                    metrics.ERRORS.labels(component="reminders").inc()
                    capture_exception(exc, component="reminders", ticket_id=item.ticket_id)
                    continue
                sent += 1
                metrics.REMINDERS.labels(result="sent").inc()
                log.info("reminder_sent")
        return sent
