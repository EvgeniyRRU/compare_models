from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from support_bot.config import Settings
from support_bot.logging import tagged
from support_bot.metrics import ERRORS
from support_bot.services.post_sync import sync_ticket
from support_bot.telegram_adapters import TelegramChat

log = structlog.get_logger()


@dataclass(slots=True)
class Runtime:
    settings: Settings
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    chat: TelegramChat

    async def sync_post(self, ticket_id: int) -> None:
        """Sync the developer-chat post after a commit. Failures are logged, not raised.

        The ticket is already saved; the background pass retries the sync later.
        """
        with tagged(ticket_id=ticket_id, component="post_sync"):
            try:
                await sync_ticket(
                    self.engine,
                    settings=self.settings,
                    sender=self.chat,
                    ticket_id=ticket_id,
                )
            except Exception:
                ERRORS.labels(component="post_sync").inc()
                log.exception("post sync failed")
