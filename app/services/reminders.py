"""Reminder scheduler: claim and render due reminders for ``new`` tickets.

The state-transition scheduling of ``next_reminder_at`` lives in
:mod:`app.services.ticket_service`; this module only *fires* reminders that have
become due. It never commits: the caller owns the transaction so that the
commit can happen *before* any reminder is sent (never inside the DB
transaction).
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.enums import TicketStatus
from app.db.models import Ticket
from app.services.render import PRIORITY_LABELS

ReminderSender = Callable[[str, int | None], Awaitable[None]]


def _now() -> datetime:
    return datetime.now(UTC)


async def claim_due_reminders(
    session: AsyncSession, *, now: datetime | None = None
) -> list[Ticket]:
    """Atomically claim all due reminders.

    Selects tickets with ``status == 'new'`` and ``next_reminder_at <= now``
    ``FOR UPDATE SKIP LOCKED``, advances each to ``now + interval(priority)``
    (NOT the overdue schedule — so there is no catch-up series after an idle
    period), and returns them. The CALLER commits the transaction and then
    sends the reminders (never inside the DB transaction). SKIP LOCKED makes
    concurrent workers disjoint (no duplicate sends).
    """
    now = now if now is not None else _now()
    result = await session.execute(
        select(Ticket)
        .where(
            Ticket.status == TicketStatus.new,
            Ticket.next_reminder_at <= now,
        )
        .order_by(Ticket.id)
        .with_for_update(skip_locked=True)
    )
    tickets = list(result.scalars())
    for ticket in tickets:
        ticket.next_reminder_at = now + timedelta(
            seconds=settings.reminder_interval_seconds(ticket.priority)
        )
    return tickets


def reminder_text(ticket: Ticket) -> str:
    """Concise Russian reminder for a due ``new`` ticket."""
    return (
        f"Напоминание: обращение #{ticket.id} не взято "
        f"(приоритет: {PRIORITY_LABELS[ticket.priority]})"
    )
