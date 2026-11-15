"""Напоминания о невзятых обращениях (SPEC «Напоминания»).

Схема: одна транзакция выбирает должные обращения (FOR UPDATE SKIP LOCKED) и
сдвигает next_reminder_at на шаг от момента снятия; только после коммита
отправляются сообщения. Не более одной попытки на срок, без догоняющих серий
после простоя.
"""

import logging
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.render import PRIORITY_LABELS
from app.config import Settings
from app.db.models import Priority, Ticket, TicketStatus
from app.metrics import REMINDERS_SENT
from app.sentry import capture_exception
from app.services.tickets import interval_expr, last_new_entry_at
from app.telegram_gateway import Gateway

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DueReminder:
    """Снимок данных для отправки после коммита транзакции снятия."""

    ticket_id: int
    priority: str
    message_id: int | None
    text: str


def format_delay(delta_minutes: int) -> str:
    """«2 ч 5 мин» / «45 мин» — человекочитаемая длительность."""
    delta_minutes = max(delta_minutes, 0)
    hours, minutes = divmod(delta_minutes, 60)
    if hours and minutes:
        return f"{hours} ч {minutes} мин"
    if hours:
        return f"{hours} ч"
    return f"{minutes} мин"


def reminder_text(ticket_id: int, priority: str, delay_minutes: int) -> str:
    try:
        label = PRIORITY_LABELS[Priority(priority)]
    except ValueError:
        label = priority
    return f"⏰ #{ticket_id} не взято {format_delay(delay_minutes)} — приоритет {label}"


async def claim_due(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    limit: int = 20,
) -> list[DueReminder]:
    """Выбрать должные напоминания и перенести сроки. Возвращает снимки для отправки."""
    claimed: list[DueReminder] = []
    async with session_factory() as session, session.begin():
        tickets = (
            (
                await session.execute(
                    select(Ticket)
                    .where(
                        Ticket.status == TicketStatus.new,
                        Ticket.next_reminder_at.is_not(None),
                        Ticket.next_reminder_at <= func.now(),
                    )
                    .order_by(Ticket.next_reminder_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        now = await session.scalar(select(func.now()))
        assert now is not None
        for ticket in tickets:
            entry_at = await last_new_entry_at(session, ticket.id)
            anchor = entry_at or ticket.created_at
            delay_minutes = int((now - anchor).total_seconds() // 60)
            claimed.append(
                DueReminder(
                    ticket_id=ticket.id,
                    priority=str(ticket.priority),
                    message_id=ticket.message_id,
                    text=reminder_text(ticket.id, str(ticket.priority), delay_minutes),
                )
            )
            # Следующий срок — от текущего времени, а не от просроченного:
            # после простоя не возникает серии догоняющих напоминаний.
            await session.execute(
                update(Ticket)
                .where(Ticket.id == ticket.id)
                .values(next_reminder_at=interval_expr(str(ticket.priority), settings))
            )
    return claimed


async def send_due(
    gateway: Gateway,
    settings: Settings,
    reminders: list[DueReminder],
) -> int:
    """Отправить снятые напоминания в чат разработчиков (reply на пост, если есть).

    Не более одной попытки на срок: ошибка логируется и уходит в Sentry, без повтора.
    """
    sent = 0
    for r in reminders:
        try:
            await gateway.send_message(
                settings.dev_chat_id,
                r.text,
                reply_to_message_id=r.message_id,
            )
        except Exception as exc:
            log.exception("Reminder send failed for ticket %s", r.ticket_id, exc_info=exc)
            capture_exception(exc)
            continue
        REMINDERS_SENT.labels(r.priority).inc()
        sent += 1
    return sent


async def sweep_once(
    gateway: Gateway,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    limit: int = 20,
) -> int:
    """Полный цикл: снятие в одной транзакции, затем отправка после коммита."""
    claimed = await claim_due(session_factory, settings, limit=limit)
    if not claimed:
        return 0
    return await send_due(gateway, settings, claimed)
