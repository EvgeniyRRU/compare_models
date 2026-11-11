import asyncio
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram import Update
from telegram.ext import Application

from support_bot.config import Settings
from support_bot.logging import tagged
from support_bot.metrics import UPDATES_DUPLICATE, UPDATES_PROCESSED
from support_bot.services.updates import run_once


async def process_update_once(
    application: Application,  # type: ignore[type-arg]
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    update: Update,
) -> bool:
    """Shared by webhook and polling: at most one attempt per update_id."""
    user = update.effective_user
    with tagged(update_id=update.update_id, telegram_user_id=user.id if user else None):

        async def handle() -> None:
            async with asyncio.timeout(settings.update_timeout_seconds):
                await application.process_update(update)

        processed = await run_once(
            session_factory,
            update_id=update.update_id,
            now=datetime.now(UTC),
            handler=handle,
        )
    if processed:
        UPDATES_PROCESSED.inc()
    else:
        UPDATES_DUPLICATE.inc()
    return processed
