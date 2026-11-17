"""Кнопки постов в чате разработчиков: callback_data t:<action>:<id>:<version>."""

from __future__ import annotations

import re
from typing import cast

from sqlalchemy import select
from telegram import Update
from telegram.ext import CallbackQueryHandler, ContextTypes

from app.bot.application import AppApplication
from app.log import get_logger
from app.models import User
from app.services import tickets as tickets_svc
from app.services.errors import AlreadyTakenError, NotFoundError, PermissionDenied, StaleTicketError

log = get_logger("bot.callbacks")

ACTION_TO_TRANSITION = {
    "take": "take",
    "wai": "to_waiting",
    "pro": "to_in_progress",
    "res": "to_resolved",
    "rej": "to_rejected",
    "dup": "to_duplicate",
    "dec": "decline",
}

PATTERN = re.compile(r"^t:(take|wai|pro|res|rej|dup|dec):(\d+):(\d+)$")


async def resync_post(app: AppApplication, ticket_id: int) -> None:
    """Пересинхронизация поста после бизнес-изменения или конфликта (SPEC)."""
    from app.services.post_sync import sync_ticket_post

    assert app.settings is not None and app.db is not None
    try:
        await sync_ticket_post(app.settings, app.db, app.bot, ticket_id)
    except Exception:
        log.exception("post resync failed; background sync will catch up", ticket_id=ticket_id)


async def on_post_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = cast(AppApplication, context.application)
    assert app.settings is not None and app.db is not None
    cq = update.callback_query
    user = update.effective_user
    assert cq is not None and user is not None
    m = PATTERN.match(cq.data or "")
    if not m:
        return
    action = m.group(1)
    ticket_id, expected_version = int(m.group(2)), int(m.group(3))
    db = app.db
    try:
        async with db.session_factory() as s, s.begin():
            user_pk = (
                await s.execute(select(User.id).where(User.telegram_id == user.id))
            ).scalar_one()
            ticket = await tickets_svc.load_ticket(s, ticket_id)
            if action == "take":
                await tickets_svc.take_ticket(s, app.settings, user_pk, ticket, expected_version)
            else:
                await tickets_svc.apply_transition(
                    s,
                    app.settings,
                    user_pk,
                    ticket,
                    expected_version,
                    ACTION_TO_TRANSITION[action],
                )
    except NotFoundError:
        await cq.answer(text="Обращение не найдено.", show_alert=True)
        return
    except AlreadyTakenError:
        await cq.answer(text="Обращение уже взято.", show_alert=True)
        await resync_post(app, ticket_id)
        return
    except PermissionDenied:
        await cq.answer(text="Недостаточно прав для этого действия.", show_alert=True)
        return
    except StaleTicketError:
        await cq.answer(text="Обращение изменилось. Пост обновлён.", show_alert=True)
        await resync_post(app, ticket_id)
        return
    await cq.answer()
    await resync_post(app, ticket_id)


def register_post_callback_handlers(app: AppApplication) -> None:
    app.add_handler(CallbackQueryHandler(on_post_callback, pattern=PATTERN))
