"""Команды: /start, /help, /cancel."""

from __future__ import annotations

from typing import cast

from sqlalchemy import select
from telegram import KeyboardButton, ReplyKeyboardMarkup, Update, WebAppInfo
from telegram.ext import CommandHandler, ContextTypes

from app.bot.application import AppApplication
from app.log import get_logger
from app.models import User
from app.services import drafts
from app.services.users import change_role

log = get_logger("bot.commands")

HELP_TEXT = (
    "Я веду учёт обращений к дежурному разработчику.\n\n"
    "/new — создать обращение\n"
    "/cancel — отменить незавершённую форму\n\n"
    "Список и карточки обращений — кнопка «📋 Обращения» (Mini App)."
)


def _menu_keyboard(url: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(text="📋 Обращения", web_app=WebAppInfo(url=url))]],
        resize_keyboard=True,
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = cast(AppApplication, context.application)
    settings, db = app.settings, app.db
    assert settings is not None and db is not None
    user = update.effective_user
    assert user is not None
    # пользователь уже upsertнут в process_update; читаем роль и при
    # необходимости назначаем bootstrap-admin
    async with db.session_factory() as s, s.begin():
        db_user = (await s.execute(select(User).where(User.telegram_id == user.id))).scalar_one()
        role = db_user.role
        if role is None and user.id in settings.bootstrap_admin_ids:
            await change_role(s, user_id=db_user.id, new_role="admin")
            role = "admin"
    chat_id = update.effective_chat.id if update.effective_chat else None
    assert chat_id is not None
    if role is None:
        await context.bot.send_message(
            chat_id,
            "Вы зарегистрированы, но у вас пока нет доступа. "
            "Обратитесь к администратору, чтобы получить роль.",
        )
        return
    await context.bot.send_message(
        chat_id,
        f"Привет, {user.first_name}! Создайте обращение командой /new "
        "или смотрите их через кнопку ниже.",
        reply_markup=_menu_keyboard(settings.mini_app_url) if settings.mini_app_url else None,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else None
    assert chat_id is not None
    await context.bot.send_message(chat_id, HELP_TEXT)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = cast(AppApplication, context.application)
    user = update.effective_user
    chat_id = update.effective_chat.id if update.effective_chat else None
    assert user is not None and chat_id is not None
    assert app.db is not None
    async with app.db.session_factory() as s, s.begin():
        user_pk = (await s.execute(select(User.id).where(User.telegram_id == user.id))).scalar_one()
        removed = await drafts.delete_draft(s, user_pk)
    await context.bot.send_message(chat_id, "Форма отменена." if removed else "Нет активной формы.")


def register_command_handlers(app: AppApplication) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
