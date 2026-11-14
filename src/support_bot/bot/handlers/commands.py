from __future__ import annotations

import structlog
from telegram import MenuButtonWebApp, Update, WebAppInfo
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from support_bot.bot.common import (
    get_services,
    no_access_text,
    role_title,
    send_step,
    upsert_user,
)
from support_bot.bot.keyboards import continue_or_restart, mini_app_keyboard
from support_bot.db.enums import UserRole
from support_bot.services import drafts as drafts_service

logger = structlog.get_logger(__name__)

HELP_TEXT = (
    "Команды:\n"
    "/new — создать обращение\n"
    "/cancel — отменить незавершённый черновик\n"
    "/help — эта справка\n\n"
    "Обращения создают support и admin. Взятие и смена статусов — "
    "developer и admin в чате разработчиков."
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.effective_chat.type != ChatType.PRIVATE:
        if update.effective_message is not None:
            await update.effective_message.reply_text(
                "Откройте бота в личных сообщениях, чтобы создать обращение."
            )
        return
    services = get_services(context)
    user = await upsert_user(update, services)
    if user is None or update.effective_message is None:
        return
    if user.role is None:
        await update.effective_message.reply_text(no_access_text())
        return

    text = (
        f"Здравствуйте, {user.display_name}!\n"
        f"Ваша роль: {role_title(user)}.\n\n"
        "Создать обращение: /new\n"
        "Справка: /help"
    )
    await update.effective_message.reply_text(text)
    if services.settings.mini_app_url:
        try:
            await context.bot.set_chat_menu_button(
                chat_id=update.effective_chat.id,
                menu_button=MenuButtonWebApp(
                    text="Обращения",
                    web_app=WebAppInfo(url=services.settings.mini_app_url),
                ),
            )
        except TelegramError:
            logger.warning("cannot set chat menu button", component="bot")
        await update.effective_message.reply_text(
            "Список обращений доступен в приложении:",
            reply_markup=mini_app_keyboard(services.settings.mini_app_url),
        )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    services = get_services(context)
    user = await upsert_user(update, services)
    if user is None:
        return
    if user.role is None:
        await update.effective_message.reply_text(no_access_text())
        return
    await update.effective_message.reply_text(HELP_TEXT)


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if (
        update.effective_chat is None
        or update.effective_chat.type != ChatType.PRIVATE
        or update.effective_message is None
    ):
        return
    services = get_services(context)
    user = await upsert_user(update, services)
    if user is None:
        return
    if user.role is None:
        await update.effective_message.reply_text(no_access_text())
        return
    if user.role not in (UserRole.SUPPORT, UserRole.ADMIN):
        await update.effective_message.reply_text(
            "Создавать обращения могут только пользователи с ролью support или admin."
        )
        return

    async with services.session_factory() as session:
        existing = await drafts_service.existing_active(
            session, user.id, ttl_hours=services.settings.draft_ttl_hours
        )
    if existing is not None:
        await update.effective_message.reply_text(
            "У вас есть незавершённый черновик обращения.",
            reply_markup=continue_or_restart(existing),
        )
        return

    async with services.session_factory() as session:
        snapshot, created = await drafts_service.create_or_get(session, user.id)
    if created:
        await send_step(context.bot, services, update.effective_chat.id, snapshot)
    else:
        await update.effective_message.reply_text(
            "У вас есть незавершённый черновик обращения.",
            reply_markup=continue_or_restart(snapshot),
        )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    if update.effective_chat is None or update.effective_chat.type != ChatType.PRIVATE:
        await update.effective_message.reply_text(
            "Отменить черновик можно только в личном чате с ботом."
        )
        return
    services = get_services(context)
    user = await upsert_user(update, services)
    if user is None:
        return
    async with services.session_factory() as session:
        deleted = await drafts_service.delete_active(session, user.id)
    if deleted:
        await update.effective_message.reply_text("Черновик обращения отменён.")
    else:
        await update.effective_message.reply_text("Активного черновика нет.")
