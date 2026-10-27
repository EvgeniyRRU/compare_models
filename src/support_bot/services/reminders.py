"""Claim deadlines before delivery so replica workers never duplicate attempts."""

from datetime import timedelta

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.contextvars import bound_contextvars

from support_bot.config import Settings
from support_bot.db.models import Ticket
from support_bot.observability.metrics import metrics
from support_bot.observability.sentry import capture_error
from support_bot.telegram.transport import Transport

logger = structlog.get_logger(__name__)


class ReminderService:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], settings: Settings, transport: Transport
    ):
        self.sessions = sessions
        self.settings = settings
        self.transport = transport

    async def claim(self) -> list[tuple[int, int | None]]:
        async with self.sessions.begin() as session:
            now = await session.scalar(select(func.now()))
            assert now is not None
            tickets = list(
                await session.scalars(
                    select(Ticket)
                    .where(Ticket.status == "new", Ticket.next_reminder_at <= now)
                    .order_by(Ticket.next_reminder_at, Ticket.id)
                    .limit(self.settings.worker_batch_size)
                    .with_for_update(skip_locked=True)
                )
            )
            claims = [(ticket.id, ticket.message_id) for ticket in tickets]
            for ticket in tickets:
                ticket.next_reminder_at = now + timedelta(
                    seconds=self.settings.reminder_seconds(ticket.priority)
                )
        return claims

    async def run_once(self) -> None:
        for ticket_id, message_id in await self.claim():
            try:
                await self.transport.send_reminder(ticket_id, message_id)
                metrics.reminders.inc()
            except Exception as error:
                logger.exception("reminder_failed", ticket_id=ticket_id, component="reminders")
                with bound_contextvars(ticket_id=ticket_id):
                    capture_error(error, component="reminders")
