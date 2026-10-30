from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Ticket
from app.domain.state_machine import Status


@dataclass(frozen=True)
class DueReminder:
    ticket_id: int
    message_id: int | None
    priority: str


async def claim_due(
    session: AsyncSession, intervals: dict[str, timedelta], limit: int = 100
) -> list[DueReminder]:
    """В одной транзакции: выбрать просроченные `new` (SKIP LOCKED) и сдвинуть срок от now().

    Коммит делает вызывающий; отправка — только после коммита.
    Технические записи: version не меняется, в журнал не пишется.
    """
    rows = (
        await session.execute(
            select(Ticket.id, Ticket.message_id, Ticket.priority)
            .where(Ticket.status == Status.NEW.value, Ticket.next_reminder_at <= func.now())
            .order_by(Ticket.next_reminder_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).all()
    for priority, interval in intervals.items():
        ids = [r.id for r in rows if r.priority == priority]
        if ids:
            await session.execute(
                update(Ticket)
                .where(Ticket.id.in_(ids))
                .values(next_reminder_at=func.now() + interval)
            )
    return [DueReminder(r.id, r.message_id, r.priority) for r in rows]
