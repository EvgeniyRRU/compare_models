from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from support_bot.config import Settings
from support_bot.db.models import Ticket
from support_bot.domain.state_machine import Status
from support_bot.services.post_render import PRIORITY_TITLES

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class ReminderJob:
    ticket_id: int
    priority: str
    reply_to_message_id: int | None


class ReminderSender(Protocol):
    async def remind(self, text: str, reply_to_message_id: int | None) -> None: ...


async def claim_due_reminders(
    engine: AsyncEngine,
    *,
    settings: Settings,
    now: datetime | None = None,
    limit: int = 50,
) -> list[ReminderJob]:
    """Move the schedule of due tickets forward and commit before anything is sent.

    The next reminder is counted from the claim time, so a long outage produces one
    reminder instead of a backlog. SKIP LOCKED lets several replicas claim in parallel
    without taking the same ticket twice.
    """
    now = now or datetime.now(UTC)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    jobs: list[ReminderJob] = []
    async with factory.begin() as session:
        rows = await session.execute(
            select(Ticket.id, Ticket.priority, Ticket.message_id)
            .where(Ticket.status == Status.NEW.value, Ticket.next_reminder_at <= now)
            .order_by(Ticket.next_reminder_at, Ticket.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        for ticket_id, priority, message_id in rows.all():
            next_at = now + timedelta(minutes=settings.reminder_interval_minutes(priority))
            await session.execute(
                update(Ticket).where(Ticket.id == ticket_id).values(next_reminder_at=next_at)
            )
            jobs.append(ReminderJob(ticket_id, priority, message_id))
    return jobs


async def run_reminder_pass(
    engine: AsyncEngine,
    *,
    settings: Settings,
    sender: ReminderSender,
    now: datetime | None = None,
    limit: int = 50,
) -> int:
    """Claim due reminders and send them. A failed send is logged and not retried."""
    jobs = await claim_due_reminders(engine, settings=settings, now=now, limit=limit)
    sent = 0
    for job in jobs:
        text = (
            f"Напоминание: обращение #{job.ticket_id} "
            f"(приоритет: {PRIORITY_TITLES[job.priority]}) ещё не взято"
        )
        try:
            await sender.remind(text, job.reply_to_message_id)
        except Exception:
            log.exception("reminder send failed", component="reminders", ticket_id=job.ticket_id)
            continue
        sent += 1
    return sent
