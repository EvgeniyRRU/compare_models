"""Long-polling mode: same dedup+dispatch path as the webhook.

Offset resumes from the highest processed update_id, so a restart never re-reads
already-processed updates from Telegram's queue. A source error does not kill the
loop; it logs, backs off a beat and retries.
"""

import asyncio
import contextlib
from collections.abc import Sequence
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram import Bot as PTBBot
from telegram import Update

from app.bot.dispatch import UpdateProcessor, dispatch_update
from app.config import Settings
from app.logging_setup import get_logger
from app.services import dedup

log = get_logger("polling")

ALLOWED_UPDATES = ["message", "callback_query", "edited_message"]


class PollingSource(Protocol):
    async def get_updates(self, offset: int) -> Sequence[Update]: ...

    async def delete_webhook(self) -> None: ...


class PtbPollingSource:
    """Real source on top of PTB Bot (created by the runtime in application.py)."""

    def __init__(self, bot: PTBBot, *, timeout_seconds: float = 30.0) -> None:
        self._bot = bot
        self._timeout = timeout_seconds

    async def get_updates(self, offset: int) -> Sequence[Update]:
        return await self._bot.get_updates(
            offset=offset,
            timeout=int(self._timeout),
            allowed_updates=ALLOWED_UPDATES,
        )

    async def delete_webhook(self) -> None:
        await self._bot.delete_webhook()


async def poll_once(
    *,
    source: PollingSource,
    processor: UpdateProcessor,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    offset: int,
) -> int:
    """One batch; returns the new offset (max seen + 1)."""
    updates = await source.get_updates(offset)
    for update in updates:
        await dispatch_update(
            update,
            processor=processor,
            session_factory=session_factory,
            settings=settings,
        )
        offset = max(offset, update.update_id + 1)
    return offset


async def run_polling(
    *,
    source: PollingSource,
    processor: UpdateProcessor,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    stop: asyncio.Event,
    idle_seconds: float = 1.0,
) -> None:
    await source.delete_webhook()
    offset = await dedup.last_update_id(session_factory) + 1
    log.info("polling_started", component="polling", offset=offset)
    while not stop.is_set():
        try:
            offset = await poll_once(
                source=source,
                processor=processor,
                session_factory=session_factory,
                settings=settings,
                offset=offset,
            )
        except Exception:
            log.exception("polling cycle failed", component="polling")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=idle_seconds)
    log.info("polling_stopped", component="polling")
