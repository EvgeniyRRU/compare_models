"""Guarded update processing shared by webhook and polling.

Order matters: dedup.reserve commits in its own short transaction BEFORE business
processing, so a crashed/timed-out handler still marks the update as seen (Telegram
retries of a known-bad update cannot loop forever), and two concurrent deliveries of
one update_id are collapsed to a single processing.
"""

import asyncio
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram import Update

from app import metrics
from app.config import Settings
from app.logging_setup import get_logger
from app.sentry import capture_exception
from app.services import dedup

log = get_logger("bot.dispatch")


class UpdateProcessor(Protocol):
    """What dispatch needs from PTB's Application (or a test stub)."""

    async def process_update(self, update: Update) -> object: ...


async def dispatch_update(
    update: Update,
    *,
    processor: UpdateProcessor,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> str:
    """Returns 'duplicate' | 'ok' | 'error'. Never raises."""
    reserved = await dedup.reserve(session_factory, update.update_id)
    if not reserved:
        metrics.UPDATES_DEDUPLICATED.inc()
        log.debug("update deduplicated", update_id=update.update_id)
        return "duplicate"
    try:
        await asyncio.wait_for(
            processor.process_update(update),
            timeout=settings.process_update_timeout_seconds,
        )
    except Exception:
        metrics.APP_ERRORS.labels("update").inc()
        capture_exception()
        log.exception("update processing failed", component="dispatch", update_id=update.update_id)
        return "error"
    metrics.UPDATES_PROCESSED.inc()
    return "ok"
