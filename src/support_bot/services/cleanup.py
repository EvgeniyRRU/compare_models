from datetime import timedelta

import structlog

from support_bot.services.drafts import DraftService
from support_bot.services.updates_dedup import UpdateDeduplicator

log = structlog.get_logger(__name__)


class CleanupService:
    def __init__(
        self, drafts: DraftService, dedup: UpdateDeduplicator, processed_updates_ttl: timedelta
    ) -> None:
        self._drafts = drafts
        self._dedup = dedup
        self._processed_ttl = processed_updates_ttl

    async def run_once(self) -> None:
        drafts = await self._drafts.delete_expired()
        updates = await self._dedup.delete_older_than(self._processed_ttl)
        if drafts or updates:
            log.info("cleanup_done", expired_drafts=drafts, processed_updates=updates)
