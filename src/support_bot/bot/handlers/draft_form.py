from __future__ import annotations

import structlog
from telegram import CallbackQuery, Update
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from support_bot.bot.callbacks import parse_draft_callback
from support_bot.bot.common import (
    BotServices,
    get_services,
    reask_current,
    send_step,
    upsert_user,
)
from support_bot.bot.keyboards import PRIORITY_ACTIONS
from support_bot.domain.errors import (
    DraftConflict,
    DraftValidationError,
    NoActiveDraft,
    PermissionDenied,
)
from support_bot.observability.metrics import observe_error
from support_bot.observability.sentry import capture_exception
from support_bot.services import drafts as drafts_service
from support_bot.services.drafts import DraftSnapshot

logger = structlog.get_logger(__name__)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None or message.text is None:
        return
    services = get_services(context)
    user = await upsert_user(update, services)
    if user is None:
        return

    async with services.session_factory() as session:
        snapshot = await drafts_service.existing_active(
            session, user.id, ttl_hours=services.settings.draft_ttl_hours
        )
    if snapshot is None:
        return

    if snapshot.step not in drafts_service.TEXT_STEPS:
        await send_step(context.bot, services, chat.id, snapshot)
        return

    reply_to = message.reply_to_message.message_id if message.reply_to_message is not None else None
    try:
        new_snapshot = await _submit_text(services, user.id, message.text, reply_to)
    except NoActiveDraft:
        return
    except DraftValidationError as exc:
        await message.reply_text(exc.message)
        await send_step(context.bot, services, chat.id, snapshot)
        return
    except DraftConflict:
        await reask_current(context.bot, services, chat.id, user.id)
        return
    await send_step(context.bot, services, chat.id, new_snapshot)


async def _submit_text(
    services: BotServices,
    user_id: int,
    text: str,
    reply_to: int | None,
) -> DraftSnapshot:
    async with services.session_factory() as session:
        return await drafts_service.submit_text(
            session, user_id=user_id, text=text, reply_to_message_id=reply_to
        )


async def on_draft_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    if query.message is None or query.message.chat.type != ChatType.PRIVATE:
        await query.answer("Форма доступна только в личном чате с ботом.", show_alert=True)
        return
    parsed = parse_draft_callback(query.data)
    if parsed is None:
        await query.answer("Кнопка устарела.", show_alert=True)
        return
    draft_id, version, action = parsed

    services = get_services(context)
    user = await upsert_user(update, services)
    if user is None:
        return
    chat_id = query.message.chat.id

    try:
        if action in PRIORITY_ACTIONS:
            async with services.session_factory() as session:
                snapshot = await drafts_service.choose_priority(
                    session,
                    user_id=user.id,
                    draft_id=draft_id,
                    version=version,
                    priority=PRIORITY_ACTIONS[action],
                )
            await _clear_markup(query)
            await send_step(context.bot, services, chat_id, snapshot)
        elif action == "s":
            async with services.session_factory() as session:
                snapshot = await drafts_service.skip_tenant(
                    session,
                    user_id=user.id,
                    draft_id=draft_id,
                    version=version,
                )
            await _clear_markup(query)
            await send_step(context.bot, services, chat_id, snapshot)
        elif action == "c":
            async with services.session_factory() as session:
                current = await drafts_service.existing_active(
                    session, user.id, ttl_hours=services.settings.draft_ttl_hours
                )
            if current is None or current.id != draft_id or current.version != version:
                raise DraftConflict("draft changed")
            await send_step(context.bot, services, chat_id, current)
        elif action == "r":
            async with services.session_factory() as session:
                snapshot = await drafts_service.restart(
                    session,
                    user_id=user.id,
                    expected_draft_id=draft_id,
                    expected_version=version,
                )
            await _clear_markup(query)
            await send_step(context.bot, services, chat_id, snapshot)
        elif action == "x":
            async with services.session_factory() as session:
                deleted = await drafts_service.delete_draft(
                    session,
                    user_id=user.id,
                    draft_id=draft_id,
                    version=version,
                )
            if not deleted:
                raise DraftConflict("draft changed")
            await _clear_markup(query)
            await context.bot.send_message(chat_id, "Черновик обращения отменён.")
        elif action == "o":
            async with services.session_factory() as session:
                ticket = await drafts_service.confirm(
                    session,
                    user_id=user.id,
                    draft_id=draft_id,
                    version=version,
                    policy=services.policy,
                )
            await _clear_markup(query)
            await context.bot.send_message(chat_id, f"Обращение #{ticket.id} создано.")
            await _sync_post(services, ticket.id)
        else:
            raise DraftConflict("unknown action")
    except DraftConflict, NoActiveDraft:
        await query.answer("Черновик изменился. Продолжите с текущего шага.", show_alert=True)
        await reask_current(context.bot, services, chat_id, user.id)
        return
    except PermissionDenied:
        await query.answer("Недостаточно прав.", show_alert=True)
        return
    except DraftValidationError as exc:
        await query.answer(exc.message, show_alert=True)
        return
    await query.answer("Готово")


async def _clear_markup(query: CallbackQuery) -> None:
    if query.message is None:
        return
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        logger.debug("cannot clear callback keyboard", component="bot")


async def _sync_post(services: BotServices, ticket_id: int) -> None:
    try:
        await services.post_sync.sync(ticket_id)
    except Exception as exc:
        logger.exception(
            "post sync after draft confirm failed",
            component="bot",
            ticket_id=ticket_id,
        )
        observe_error("bot")
        capture_exception(exc, component="bot", ticket_id=ticket_id)
