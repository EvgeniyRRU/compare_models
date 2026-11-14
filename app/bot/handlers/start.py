"""``/start`` and ``/help`` command handlers."""

from typing import Any, cast

from sqlalchemy import select
from telegram import Update
from telegram.ext import ContextTypes

from app.config import settings
from app.db.enums import Role
from app.db.models import User
from app.db.session import async_session_factory


async def _reply(update: Update, text: str) -> None:
    message = update.effective_message
    if message is not None:
        await message.reply_text(text)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/start``: bootstrap roleless admins, then greet.

    The user-identification middleware has already registered the user and stashed a
    per-update snapshot at ``context.user_data["user"]``. We re-read the row fresh here
    so the bootstrap decision sees committed role state, and only promote a roleless
    user whose telegram id is a bootstrap admin id — an existing role is never changed.
    """
    snapshot = cast(dict[str, Any], context.user_data).get("user")
    if snapshot is None:  # middleware guarantees a user; nothing sensible to do otherwise
        return

    role: Role | None = None
    async with async_session_factory() as session:
        db_user = await session.scalar(select(User).where(User.telegram_id == snapshot.telegram_id))
        if db_user is not None:
            if db_user.role is None and db_user.telegram_id in settings.bootstrap_admin_ids:
                db_user.role = Role.admin
                await session.commit()
            role = db_user.role

    if role is None:
        await _reply(update, "Обратитесь к администратору для получения доступа к боту.")
        return

    await _reply(
        update,
        "Добро пожаловать!\n\nДоступные команды:\n/help — справка\n/new — создать обращение",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/help``: list the bot's commands."""
    await _reply(
        update,
        "Команды бота:\n"
        "/new — создать новое обращение\n"
        "/cancel — отменить создание обращения\n\n"
        "Действия с обращениями (взять, решить, отклонить и т.д.) выполняются "
        "кнопками в чате поддержки.",
    )
