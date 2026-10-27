"""The same committed update ingestion path is used by webhook and polling."""

import asyncio

from structlog.contextvars import bound_contextvars
from telegram import Update
from telegram.ext import Application

from support_bot.config import Settings
from support_bot.observability.sentry import capture_error
from support_bot.services.updates import UpdateService


class UpdateProcessor:
    def __init__(self, application: Application, updates: UpdateService, settings: Settings):
        self.application = application
        self.updates = updates
        self.settings = settings

    async def process(self, update: Update) -> bool:
        user = update.effective_user
        with bound_contextvars(
            update_id=update.update_id,
            telegram_user_id=user.id if user else None,
            component="telegram",
            ticket_id=None,
        ):
            # Claim failures propagate: a database outage must not acknowledge
            # an update before its durable claim exists.
            if not await self.updates.claim(update.update_id):
                return False
            try:
                async with asyncio.timeout(self.settings.update_timeout_seconds):
                    await self.application.process_update(update)
            except Exception as error:
                capture_error(error, component="telegram_update")
            return True


async def polling_loop(application: Application, processor: UpdateProcessor) -> None:
    offset: int | None = None
    await application.bot.delete_webhook(drop_pending_updates=False)
    while True:
        try:
            updates = await application.bot.get_updates(
                offset=offset,
                timeout=20,
                allowed_updates=["message", "callback_query"],
                read_timeout=25,
            )
            for update in updates:
                await processor.process(update)
                offset = update.update_id + 1
        except Exception as error:
            capture_error(error, component="polling")
            await asyncio.sleep(2)
