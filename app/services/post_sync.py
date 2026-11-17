"""Синхронизация поста обращения в чате разработчиков (SPEC «Публикация в чат разработчиков»).

Источник истины — БД; пост — её отображение. Сериализация между репликами —
session-level advisory lock на выделенном физическом соединении (lock_engine,
NullPool). Technical-операция: tickets.version НЕ увеличивается.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any, cast

import telegram
from sqlalchemy import select, text, update

from app.config import Settings
from app.db import DB
from app.log import get_logger
from app.models import Ticket, User

log = get_logger("post_sync")

MAX_POST_LEN = 4096

STATUS_RU = {
    "new": "новое",
    "in_progress": "в работе",
    "waiting_info": "ждём информацию",
    "resolved": "решено",
    "rejected": "отклонено",
    "duplicate": "дубль",
}
PRIORITY_RU = {"critical": "критический", "high": "высокий", "medium": "средний", "low": "низкий"}


def format_user(user: User) -> str:
    name, username = user.first_name, user.username
    if name and username:
        return f"{name} (@{username})"
    if name:
        return name
    if username:
        return f"@{username}"
    return f"ID {user.telegram_id}"


def render_post(ticket: Ticket, author: User, assignee: User | None) -> str:
    """Текст поста. Гарантированно <= 4096: описание обрезается с «…»."""
    header = (
        f"Обращение #{ticket.id}\n"
        f"Приоритет: {PRIORITY_RU[ticket.priority]}\n"
        f"Заявка: {ticket.request_id}\n"
        f"Арендатор: {ticket.tenant_id or '—'}\n"
        f"Автор: {format_user(author)}"
    )
    footer = (
        f"Статус: {STATUS_RU[ticket.status]}\n"
        f"Исполнитель: {format_user(assignee) if assignee else '—'}"
    )
    description = ticket.description
    total = f"{header}\n\n{description}\n\n{footer}"
    if len(total) > MAX_POST_LEN:
        overhead = len(header) + len(footer) + 4
        description = description[: MAX_POST_LEN - overhead - 1].rstrip() + "…"
        total = f"{header}\n\n{description}\n\n{footer}"
    return total


async def bot_api_call(
    func: Callable[..., Any], *args: Any, retries: int = 5, **kwargs: Any
) -> Any:
    """Вызов метода бота с ретраями: 429 — backoff с учётом retry_after."""
    for attempt in range(retries):
        try:
            return await func(*args, **kwargs)
        except telegram.error.RetryAfter as e:
            retry_after = e.retry_after
            seconds = retry_after if isinstance(retry_after, int) else retry_after.total_seconds()
            wait = seconds + attempt
            log.warning("telegram 429, повтор через %.1f с", wait)
            await asyncio.sleep(wait)
    raise RuntimeError("Превышено число ретраев Telegram API")


async def _advisory_lock(conn: Any, key: int, timeout: float) -> bool:  # noqa: ASYNC109
    """pg_try_advisory_lock на выделенном соединении. timeout <= 0 — одна попытка (skip)."""
    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        got = (await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})).scalar_one()
        if got:
            return True
        if timeout <= 0 or time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.25)


async def _advisory_unlock(conn: Any, key: int) -> None:
    await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})


async def sync_ticket_post(
    settings: Settings,
    db: DB,
    bot: Any,
    ticket_id: int,
    *,
    force_new: bool = False,
    lock_timeout: float = 30.0,
) -> None:
    """Одна процедура синхронизации поста (SPEC, пункты 1–5).

    lock_timeout <= 0: блокировку не ждать — пропустить (фоновый режим).
    Блокировка снимается в finally при любой ситуации (ошибка, таймаут, cancel);
    при неудаче снятия соединение закрывается (NullPool → физически), и блокировка
    умирает вместе с сессией БД.
    """
    conn = await db.lock_engine.connect()
    locked = False
    try:
        locked = await _advisory_lock(conn, ticket_id, lock_timeout)
        if not locked:
            log.warning("post sync skipped: advisory lock busy", ticket_id=ticket_id)
            return
        await _do_sync(settings, db, bot, ticket_id, force_new=force_new)
    finally:
        try:
            if locked:
                try:
                    await _advisory_unlock(conn, ticket_id)
                except Exception:
                    log.exception("advisory unlock failed; closing connection", ticket_id=ticket_id)
        finally:
            await conn.close()


async def _do_sync(
    settings: Settings, db: DB, bot: Any, ticket_id: int, *, force_new: bool
) -> None:
    # 2. Под блокировкой — свежее состояние из БД; рендерим ТОЛЬКО из него.
    async with db.session_factory() as session:
        ticket = await session.get(Ticket, ticket_id)
        if ticket is None:
            return
        author = cast(User, await session.get(User, ticket.author_id))
        assignee = await session.get(User, ticket.assignee_id) if ticket.assignee_id else None
        rendered_version = ticket.version
        post_text = render_post(ticket, author, assignee)
        message_id: int | None = None if force_new else ticket.message_id
        dev_chat = settings.dev_chat_id

    # 3. Отправить новый либо отредактировать существующий.
    if message_id is None:
        sent = await bot_api_call(bot.send_message, dev_chat, post_text)
        message_id = sent.message_id
    else:
        try:
            await bot_api_call(
                bot.edit_message_text, post_text, chat_id=dev_chat, message_id=message_id
            )
        except telegram.error.BadRequest as e:
            msg = str(e).lower()
            if "message is not modified" in msg:
                pass  # пост уже актуален — считаем успехом
            elif "not found" in msg or "can't edit" in msg or "no rights" in msg:
                # пост удалён/недоступен — публикуем новый
                sent = await bot_api_call(bot.send_message, dev_chat, post_text)
                message_id = sent.message_id
            else:
                raise

    # 4. Одной транзакцией: post_synced_version = отрендеренная version
    #    (НЕ текущая: если состояние изменилось, фон увидит отставание и повторит).
    async with db.session_factory() as session, session.begin():
        values: dict[str, Any] = {"post_synced_version": rendered_version}
        stored_message_id = (
            await session.execute(select(Ticket.message_id).where(Ticket.id == ticket_id))
        ).scalar_one()
        if stored_message_id != message_id:
            values["message_id"] = message_id
        await session.execute(update(Ticket).where(Ticket.id == ticket_id).values(**values))
    # 5. Блокировку снимает sync_ticket_post в finally.
