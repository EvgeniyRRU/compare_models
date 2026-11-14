from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import structlog
from telegram import Bot, Message, Update
from telegram.ext import ContextTypes

from support_bot.config import Settings
from support_bot.db.models.user import User
from support_bot.db.session import SessionFactory
from support_bot.domain.labels import ROLE_LABELS
from support_bot.domain.reminders import ReminderPolicy
from support_bot.services import drafts as drafts_service
from support_bot.services import users as users_service
from support_bot.services.drafts import DraftSnapshot
from support_bot.services.post_sync import PostSyncService
from support_bot.services.users import TelegramProfile

SERVICES_KEY = "services"


@dataclass(slots=True)
class BotServices:
    settings: Settings
    session_factory: SessionFactory
    post_sync: PostSyncService
    policy: ReminderPolicy


def get_services(context: ContextTypes.DEFAULT_TYPE) -> BotServices:
    return cast(BotServices, context.application.bot_data[SERVICES_KEY])


def profile_from_user(update: Update) -> TelegramProfile | None:
    user = update.effective_user
    if user is None:
        return None
    return TelegramProfile(
        telegram_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
    )


async def upsert_user(update: Update, services: BotServices) -> User | None:
    profile = profile_from_user(update)
    if profile is None:
        return None
    structlog.contextvars.bind_contextvars(telegram_user_id=profile.telegram_id)
    async with services.session_factory() as session:
        return await users_service.upsert_telegram_user(
            session, profile, services.settings.bootstrap_admin_ids
        )


def role_title(user: User) -> str:
    if user.role is None:
        return "нет роли"
    return ROLE_LABELS[user.role]


async def send_step(
    bot: Bot,
    services: BotServices,
    chat_id: int,
    snapshot: DraftSnapshot,
) -> Message:
    from support_bot.bot.keyboards import render_step

    text, markup = render_step(snapshot)
    message = await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=markup,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    async with services.session_factory() as session:
        await drafts_service.set_prompt_message_id(
            session,
            draft_id=snapshot.id,
            version=snapshot.version,
            step=snapshot.step,
            message_id=message.message_id,
        )
    return message


async def reask_current(
    bot: Bot, services: BotServices, chat_id: int, user_id: int
) -> DraftSnapshot | None:
    async with services.session_factory() as session:
        snapshot = await drafts_service.existing_active(
            session, user_id, ttl_hours=services.settings.draft_ttl_hours
        )
    if snapshot is None:
        await bot.send_message(
            chat_id,
            "Активного черновика нет. Начните создание обращения командой /new.",
        )
        return None
    await send_step(bot, services, chat_id, snapshot)
    return snapshot


def no_access_text() -> str:
    return "Доступ не выдан. Обратитесь к администратору, чтобы он назначил вам роль в приложении."
