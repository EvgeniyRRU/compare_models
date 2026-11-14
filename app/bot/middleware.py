"""User-identification middleware for the Telegram bot."""

from typing import Any, cast

from sqlalchemy import select
from telegram import Update
from telegram.ext import ContextTypes

from app.db.models import User
from app.db.session import async_session_factory


async def register_user_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Load-or-create the Telegram user and stash it on ``context.user_data["user"]``.

    Runs before every handler: identifies the user by telegram_id, registers a NEW
    user without a role, and refreshes username/full_name on every interaction.
    The stored ``User`` is a per-update snapshot (re-read each update); ``role`` is
    used for UX only — services re-read the role in-transaction for authority.
    """
    user = update.effective_user
    if user is None:
        return

    async with async_session_factory() as session:
        db_user = await session.scalar(select(User).where(User.telegram_id == user.id))
        if db_user is None:
            db_user = User(
                telegram_id=user.id,
                username=user.username,
                full_name=user.full_name,
            )
            session.add(db_user)
        elif db_user.username != user.username or db_user.full_name != user.full_name:
            db_user.username = user.username
            db_user.full_name = user.full_name
        await session.commit()

    cast(dict[str, Any], context.user_data)["user"] = db_user
