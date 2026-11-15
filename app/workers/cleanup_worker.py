"""Periodic housekeeping: prune dedup rows and expired drafts."""

import asyncio
import contextlib

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.logging_setup import get_logger
from app.services import dedup, drafts

log = get_logger("workers.cleanup")


async def sweep_once(session_factory: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    removed_updates = await dedup.cleanup(session_factory, settings.processed_updates_ttl_hours)
    removed_drafts = await drafts.cleanup_expired(session_factory, settings.draft_ttl_hours)
    if removed_drafts:
        log.info("draft_cleanup", component="cleanup", removed=removed_drafts)
    if removed_updates:
        log.debug("dedup_cleanup", component="cleanup", removed=removed_updates)


async def run_cleanup_worker(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    stop: asyncio.Event,
    *,
    interval_seconds: float = 3600.0,
) -> None:
    while not stop.is_set():
        try:
            await sweep_once(session_factory, settings)
        except Exception:
            log.exception("cleanup sweep failed", component="cleanup")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
