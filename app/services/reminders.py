from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from app.db.models import Ticket, TicketStatus
from app.db.session import SessionLocal
from app.services.tickets import reminder_minutes


async def claim_due_reminders() -> list[tuple[int, int | None]]:
    due: list[tuple[int, int | None]] = []
    async with SessionLocal.begin() as session:
        tickets = (
            await session.scalars(
                select(Ticket)
                .where(
                    Ticket.status == TicketStatus.NEW,
                    Ticket.next_reminder_at <= datetime.now(UTC),
                )
                .order_by(Ticket.next_reminder_at)
                .limit(100)
                .with_for_update(skip_locked=True)
            )
        ).all()
        now = datetime.now(UTC)
        for ticket in tickets:
            await session.execute(
                update(Ticket)
                .where(Ticket.id == ticket.id, Ticket.status == TicketStatus.NEW)
                .values(
                    next_reminder_at=now + timedelta(minutes=reminder_minutes(ticket.priority)),
                    updated_at=now,
                )
            )
            due.append((ticket.id, ticket.message_id))
    return due


async def send_due_reminders(bot, chat_id: int) -> int:
    due = await claim_due_reminders()
    from app.core.metrics import REMINDERS_SENT

    for ticket_id, message_id in due:
        REMINDERS_SENT.inc()
        try:
            if message_id is None:
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"Напоминание: обращение #{ticket_id} всё ещё ожидает исполнителя.",
                )
                continue
            await bot.send_message(
                chat_id=chat_id,
                text=f"Напоминание: обращение #{ticket_id} всё ещё ожидает исполнителя.",
                reply_to_message_id=message_id,
            )
        except Exception:
            import sentry_sdk
            import structlog

            structlog.get_logger().exception("reminder_send_failed", ticket_id=ticket_id)
            sentry_sdk.capture_exception()
    return len(due)
