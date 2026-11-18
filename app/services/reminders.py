"""Напоминания о невзятых обращениях (SPEC «Напоминания»).

Не более одной попытки отправки на каждый срок; потеря при сбое допустима.
Расписание в БД (next_reminder_at) + FOR UPDATE SKIP LOCKED — нет дублей
между репликами. In-memory JobQueue PTB не используется.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import sentry_sdk
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.log import get_logger
from app.metrics import reminders_sent_total
from app.models import Ticket
from app.services.post_sync import PRIORITY_RU, bot_api_call

log = get_logger("reminders")


@dataclass(frozen=True)
class ReminderClaim:
    ticket_id: int
    priority: str
    message_id: int | None


def _now() -> datetime:
    return datetime.now(UTC)


async def claim_due_reminders(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings, limit: int = 50
) -> list[ReminderClaim]:
    """Одна транзакция: SELECT ... FOR UPDATE SKIP LOCKED + сдвиг next_reminder_at + коммит.

    Следующий срок считается от текущего времени — после простоя нет серии
    догоняющих напоминаний.
    """
    now = _now()
    async with session_factory() as session, session.begin():
        tickets = (
            (
                await session.execute(
                    select(Ticket)
                    .where(
                        Ticket.status == "new",
                        Ticket.next_reminder_at.is_not(None),
                        Ticket.next_reminder_at <= now,
                    )
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        claims: list[ReminderClaim] = []
        for t in tickets:
            t.next_reminder_at = now + timedelta(seconds=settings.reminder_interval(t.priority))
            claims.append(
                ReminderClaim(ticket_id=t.id, priority=t.priority, message_id=t.message_id)
            )
    return claims


async def send_reminders(settings: Settings, bot: Any, claims: list[ReminderClaim]) -> None:
    """После коммита: ответ на пост (если пост есть).

    Ошибка — лог + Sentry, без повторной отправки.
    """
    for claim in claims:
        try:
            text = (
                f"⏰ Напоминание: обращение #{claim.ticket_id} "
                f"({PRIORITY_RU[claim.priority]}) всё ещё не взято."
            )
            await bot_api_call(
                bot.send_message, settings.dev_chat_id, text, reply_to_message_id=claim.message_id
            )
            reminders_sent_total.labels(result="ok").inc()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("reminder send failed", ticket_id=claim.ticket_id)
            reminders_sent_total.labels(result="error").inc()
            sentry_sdk.capture_exception(exc)
