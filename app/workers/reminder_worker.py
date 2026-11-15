"""Фоновый воркер напоминаний: периодические sweep_once до стопа."""

import asyncio
import contextlib
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.services.reminders import sweep_once
from app.telegram_gateway import Gateway

logger = logging.getLogger(__name__)


async def run_reminder_worker(
    gateway: Gateway,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    stop: asyncio.Event,
    *,
    interval_seconds: float | None = None,
) -> None:
    """Loop sweep until `stop` is set; first pass runs immediately."""
    interval = interval_seconds or settings.reminder_poll_interval_seconds
    while not stop.is_set():
        try:
            await sweep_once(gateway, session_factory, settings)
        except Exception:
            logger.exception("reminder sweep crashed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)
