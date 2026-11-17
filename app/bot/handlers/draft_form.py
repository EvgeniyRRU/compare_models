"""/new, текстовые ответы формы и кнопки (d:*)."""

from __future__ import annotations

import uuid
from typing import cast

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from app.bot import form
from app.bot.application import AppApplication
from app.log import get_logger
from app.models import Draft, User
from app.services import drafts
from app.services.drafts import DraftResult
from app.services.errors import DraftValidationError, StaleAnswerError, StaleDraftError
from app.states import CREATE_ROLES

log = get_logger("bot.draft_form")


async def _ask_question(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, draft: Draft
) -> None:
    """Отправить вопрос шага и сохранить message_id условным обновлением."""
    app = cast(AppApplication, context.application)
    assert app.db is not None
    db = app.db
    chat_id = update.effective_chat.id if update.effective_chat is not None else None
    assert chat_id is not None
    text, markup = form.question_for(draft.step, draft)
    msg = await context.bot.send_message(chat_id, text, reply_markup=markup)
    if draft.step in form.TEXT_STEPS:
        await drafts.save_prompt_message_id(
            db.session_factory, draft.id, draft.version, draft.step, msg.message_id
        )


async def _show_stale(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """Устаревшая кнопка: показать актуальный вопрос."""
    app = cast(AppApplication, context.application)
    assert app.db is not None
    db = app.db
    async with db.session_factory() as s:
        d = await drafts.get_draft(s, user_id)
    chat_id = update.effective_chat.id if update.effective_chat is not None else None
    assert chat_id is not None
    if d is None:
        await context.bot.send_message(chat_id, "Черновик не найден. Начните заново: /new")
        return
    await _ask_question(update, context, user_id, d)


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = cast(AppApplication, context.application)
    db = app.db
    assert db is not None
    user = update.effective_user
    chat = update.effective_chat.id if update.effective_chat is not None else None
    assert user is not None and chat is not None
    async with db.session_factory() as s:
        db_user = (await s.execute(select(User).where(User.telegram_id == user.id))).scalar_one()
        db_user_id, role = db_user.id, db_user.role
    if role not in CREATE_ROLES:
        await context.bot.send_message(
            chat, "Создавать обращения могут только первая линия (support) и администраторы."
        )
        return
    async with db.session_factory() as s, s.begin():
        draft, created = await drafts.start_draft(s, db_user_id)
    if created:
        await _ask_question(update, context, db_user_id, draft)
        return
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Продолжить", callback_data=form.cb("cont", draft)),
                InlineKeyboardButton("Начать заново", callback_data=form.cb("reset", draft)),
            ]
        ]
    )
    await context.bot.send_message(
        chat, "У вас есть незавершённая форма. Продолжить или начать заново?", reply_markup=keyboard
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = cast(AppApplication, context.application)
    db = app.db
    assert db is not None
    user = update.effective_user
    message = update.effective_message
    chat = update.effective_chat.id if update.effective_chat is not None else None
    assert user is not None and message is not None and chat is not None
    text_value = message.text or ""
    reply_to = message.reply_to_message.message_id if message.reply_to_message else None
    async with db.session_factory() as s:
        db_user_id = (
            await s.execute(select(User.id).where(User.telegram_id == user.id))
        ).scalar_one()
        draft = await drafts.get_draft(s, db_user_id)
    if draft is None:
        await context.bot.send_message(chat, "Активной формы нет. Создайте её командой /new.")
        return
    try:
        async with db.session_factory() as s, s.begin():
            result = await drafts.answer_text(s, db_user_id, text_value, reply_to)
    except StaleAnswerError:
        # ответ без нужной привязки: данные не меняются, заново задаём вопрос
        await _ask_question(update, context, db_user_id, draft)
        return
    except DraftValidationError as e:
        await context.bot.send_message(
            chat, f"Некорректное значение {e.field}. Попробуйте ещё раз."
        )
        return
    draft.step, draft.version = cast(str, result.step), cast(int, result.version)
    await _ask_question(update, context, db_user_id, draft)


async def _after_choice(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, result: DraftResult
) -> None:
    """Общий код после answer_choice: пост/подтверждение или вопрос нового шага."""
    app = cast(AppApplication, context.application)
    chat = update.effective_chat.id if update.effective_chat is not None else None
    assert chat is not None
    if result.ticket_id is not None:
        from app.services.post_sync import sync_ticket_post

        # пост публикуется ПОСЛЕ коммита создания (сбой публикации не теряет обращение)
        assert app.settings is not None and app.db is not None
        try:
            await sync_ticket_post(app.settings, app.db, context.bot, result.ticket_id)
        except Exception:
            log.exception("post publish failed after draft send; background sync will catch up")
        await context.bot.send_message(chat, f"Обращение #{result.ticket_id} создано.")
        return
    if result.step is None:
        await context.bot.send_message(chat, "Форма отменена.")
        return
    assert app.db is not None
    db = app.db
    async with db.session_factory() as s:
        d = await drafts.get_draft(s, user_id)
    if d is not None:
        await _ask_question(update, context, user_id, d)


async def on_draft_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = cast(AppApplication, context.application)
    settings, db = app.settings, app.db
    assert settings is not None and db is not None
    user = update.effective_user
    cq = update.callback_query
    assert user is not None and cq is not None
    parsed = form.parse_draft_callback(cq.data or "")
    if parsed is None:
        return
    kind, uid_str, version, extra = parsed
    draft_id = uuid.UUID(uid_str)
    async with db.session_factory() as s:
        db_user_id = (
            await s.execute(select(User.id).where(User.telegram_id == user.id))
        ).scalar_one()

    if kind == "cont":
        async with db.session_factory() as s:
            d = await drafts.get_draft(s, db_user_id)
        if d is None or d.id != draft_id or d.version != version:
            await cq.answer(text="Форма изменилась. Продолжите с текущего шага.", show_alert=True)
            await _show_stale(update, context, db_user_id)
            return
        await cq.answer()
        await _ask_question(update, context, db_user_id, d)
        return

    if kind == "reset":
        try:
            async with db.session_factory() as s, s.begin():
                d = await drafts.reset_draft(s, db_user_id, draft_id, version)
        except StaleDraftError:
            await cq.answer(text="Форма изменилась. Продолжите с текущего шага.", show_alert=True)
            await _show_stale(update, context, db_user_id)
            return
        await cq.answer()
        await _ask_question(update, context, db_user_id, d)
        return

    # prio / skip / send / canx
    choice = {"prio": "prio", "skip": "skip", "send": "send", "canx": "canx"}.get(kind)
    if choice is None:
        await cq.answer()
        return
    value = form.SHORT_PRIORITY.get(extra) if (kind == "prio" and extra is not None) else None
    try:
        async with db.session_factory() as s, s.begin():
            result = await drafts.answer_choice(
                s, settings, db_user_id, draft_id, version, choice, value
            )
    except StaleDraftError:
        log.info("stale draft callback rejected", kind=kind, user_id=user.id)
        await cq.answer(text="Форма изменилась. Продолжите с текущего шага.", show_alert=True)
        await _show_stale(update, context, db_user_id)
        return
    await cq.answer()
    await _after_choice(update, context, db_user_id, result)


def register_draft_form_handlers(app: AppApplication) -> None:
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_draft_callback, pattern=r"^d:"))
