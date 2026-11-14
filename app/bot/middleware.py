"""User-identification middleware for the Telegram bot."""

from typing import Any, cast

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
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
        db_user = (
            await session.scalars(
                pg_insert(User)
                .values(telegram_id=user.id, username=user.username, full_name=user.full_name)
                .on_conflict_do_update(
                    index_elements=[User.telegram_id],
                    set_={
                        "username": user.username,
                        "full_name": user.full_name,
                        "updated_at": func.now(),  # Core upsert bypasses ORM onupdate
                    },
                    where=or_(
                        User.username.is_distinct_from(user.username),
                        User.full_name.is_distinct_from(user.full_name),
                    ),
                )
                .returning(User)
            )
        ).one_or_none()
        if db_user is None:
            # The row already exists and is unchanged (or was concurrently
            # created): the ``WHERE`` clause turned ``ON CONFLICT`` into a no-op,
            # so ``RETURNING`` yields nothing. Re-read the committed row.
            db_user = await session.scalar(select(User).where(User.telegram_id == user.id))
        await session.commit()

    cast(dict[str, Any], context.user_data)["user"] = db_user
