from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from support_bot.db.enums import TicketPriority, TicketStatus
from support_bot.db.models.ticket import Ticket
from support_bot.db.session import SessionFactory
from support_bot.domain.labels import PRIORITY_LABELS
from support_bot.domain.reminders import ReminderPolicy
from support_bot.services.tickets import utcnow


@dataclass(frozen=True, slots=True)
class DueReminder:
    ticket_id: int
    priority: TicketPriority
    message_id: int | None


async def claim_due_reminders(
    session_factory: SessionFactory,
    policy: ReminderPolicy,
    *,
    batch_size: int = 50,
    now: datetime | None = None,
) -> list[DueReminder]:
    """
    Lock due 'new' tickets, move their next reminder forward and commit.

    The next deadline is calculated from the current time, so a long downtime
    never produces a burst of catch-up reminders.
    """
    now = now or utcnow()
    async with session_factory() as session, session.begin():
        tickets = list(
            (
                await session.scalars(
                    select(Ticket)
                    .where(
                        Ticket.status == TicketStatus.NEW,
                        Ticket.next_reminder_at.is_not(None),
                        Ticket.next_reminder_at <= now,
                    )
                    .order_by(Ticket.next_reminder_at)
                    .limit(batch_size)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        reminders = [
            DueReminder(
                ticket_id=ticket.id,
                priority=ticket.priority,
                message_id=ticket.message_id,
            )
            for ticket in tickets
        ]
        for ticket in tickets:
            ticket.next_reminder_at = now + policy.interval_for(ticket.priority)
    return reminders


def render_reminder_text(ticket_id: int, priority: TicketPriority) -> str:
    return (
        f"⏰ <b>#{ticket_id}</b> · {PRIORITY_LABELS[priority]}\n"
        "Обращение всё ещё не взято. Напоминание для дежурного разработчика."
    )
