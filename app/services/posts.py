import asyncio
import logging

import sentry_sdk
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, NetworkError, RetryAfter

from app.db.models import Ticket, User
from app.metrics import ERRORS

log = logging.getLogger(__name__)
STATUS = {
    "new": "Новое",
    "in_progress": "В работе",
    "waiting_info": "Ожидает информации",
    "resolved": "Решено",
    "rejected": "Отклонено",
    "duplicate": "Дубль",
}


def render_post(
    ticket: Ticket, author: str, assignee: str | None
) -> tuple[str, InlineKeyboardMarkup | None]:
    prefix = f"Обращение #{ticket.id}\nПриоритет: {ticket.priority}\nЗаявка: {ticket.request_id}\nАрендатор: {ticket.tenant_id or '—'}\nАвтор: {author}\nСтатус: {STATUS[ticket.status]}\nИсполнитель: {assignee or '—'}\n\nОписание:\n"
    available = max(0, 4096 - len(prefix))
    description = ticket.description
    if len(description) > available:
        description = description[: max(0, available - 1)] + "…"
    body = (prefix + description)[:4096]
    buttons = []
    if ticket.status == "new":
        buttons = [
            [InlineKeyboardButton("Взять", callback_data=f"t:take:{ticket.id}:{ticket.version}")]
        ]
    elif ticket.status in {"in_progress", "waiting_info"}:
        toggle = (
            ("В ожидании информации", "wait")
            if ticket.status == "in_progress"
            else ("В работу", "work")
        )
        buttons = [
            [
                InlineKeyboardButton(
                    toggle[0], callback_data=f"t:{toggle[1]}:{ticket.id}:{ticket.version}"
                )
            ],
            [
                InlineKeyboardButton(
                    label, callback_data=f"t:{action}:{ticket.id}:{ticket.version}"
                )
                for label, action in [
                    ("Решено", "resolve"),
                    ("Отклонено", "reject"),
                    ("Дубль", "dup"),
                ]
            ],
            [
                InlineKeyboardButton(
                    "Отказаться", callback_data=f"t:release:{ticket.id}:{ticket.version}"
                )
            ],
        ]
    return body, InlineKeyboardMarkup(buttons) if buttons else None


async def _call(operation):
    for attempt in range(4):
        try:
            return await operation()
        except RetryAfter as exc:
            delay = (
                exc.retry_after.total_seconds()
                if hasattr(exc.retry_after, "total_seconds")
                else float(exc.retry_after)
            )
            await asyncio.sleep(delay)
        except NetworkError:
            if attempt == 3:
                raise
            await asyncio.sleep(2**attempt)
    raise RuntimeError("Telegram retry limit exceeded")


async def sync_post(
    engine: AsyncEngine,
    bot,
    chat_id: int,
    ticket_id: int,
    *,
    force: bool = False,
    try_lock: bool = False,
) -> bool:
    conn = await engine.connect()
    locked = False
    acquiring = False
    try:
        acquiring = True
        if try_lock:
            locked = bool(
                await conn.scalar(text("SELECT pg_try_advisory_lock(:id)"), {"id": ticket_id})
            )
        else:
            async with asyncio.timeout(10):
                await conn.execute(text("SELECT pg_advisory_lock(:id)"), {"id": ticket_id})
                locked = True
        acquiring = False
        if not locked:
            return False
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            ticket = await session.get(Ticket, ticket_id)
            if ticket is None:
                return False
            author = await session.get(User, ticket.author_id)
            assignee = await session.get(User, ticket.assignee_id) if ticket.assignee_id else None
            body, keyboard = render_post(
                ticket, author.name if author else "—", assignee.name if assignee else None
            )
            version, message_id = ticket.version, ticket.message_id
        sent_id = None
        if message_id and not force:
            try:
                await _call(
                    lambda: bot.edit_message_text(
                        chat_id=chat_id, message_id=message_id, text=body, reply_markup=keyboard
                    )
                )
            except BadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    pass
                elif any(
                    s in str(exc).lower()
                    for s in (
                        "message to edit not found",
                        "message can't be edited",
                        "message identifier is not specified",
                    )
                ):
                    sent = await _call(
                        lambda: bot.send_message(chat_id=chat_id, text=body, reply_markup=keyboard)
                    )
                    sent_id = sent.message_id
                else:
                    raise
        else:
            sent = await _call(
                lambda: bot.send_message(chat_id=chat_id, text=body, reply_markup=keyboard)
            )
            sent_id = sent.message_id
        values = {"post_synced_version": version}
        if sent_id is not None:
            values["message_id"] = sent_id
        async with sessions.begin() as session:
            await session.execute(update(Ticket).where(Ticket.id == ticket_id).values(**values))
        return True
    finally:
        if acquiring:
            await conn.invalidate()
        if locked:
            try:
                result = await asyncio.shield(
                    conn.scalar(text("SELECT pg_advisory_unlock(:id)"), {"id": ticket_id})
                )
                if not result:
                    await conn.invalidate()
            except BaseException:
                await conn.invalidate()
                raise
        await conn.close()


async def sync_pending(engine: AsyncEngine, bot, chat_id: int) -> None:
    sessions = async_sessionmaker(engine)
    async with sessions() as session:
        ids = (
            await session.scalars(
                select(Ticket.id)
                .where(
                    (Ticket.message_id.is_(None)) | (Ticket.post_synced_version < Ticket.version)
                )
                .limit(100)
            )
        ).all()
    for ticket_id in ids:
        try:
            await sync_post(engine, bot, chat_id, ticket_id, try_lock=True)
        except Exception as exc:
            ERRORS.labels("post_sync").inc()
            log.exception("Failed to synchronize ticket %s", ticket_id)
            sentry_sdk.capture_exception(exc)
