import asyncio
from collections.abc import Awaitable, Callable

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram.ext import Application

from support_bot.config import Settings
from support_bot.metrics import ERRORS, REMINDERS_SENT
from support_bot.runtime import Runtime
from support_bot.services import drafts, updates
from support_bot.services.post_sync import sync_pending
from support_bot.services.reminders import run_reminder_pass

log = structlog.get_logger()
CLEANUP_INTERVAL_SECONDS = 3600


async def run_periodic(
    name: str, interval_seconds: float, job: Callable[[], Awaitable[object]]
) -> None:
    """Run `job` forever. A failing run is logged and the loop keeps going."""
    while True:
        try:
            await job()
        except asyncio.CancelledError:
            raise
        except Exception:
            ERRORS.labels(component=name).inc()
            log.exception("worker run failed", component=name)
        await asyncio.sleep(interval_seconds)


async def post_sync_job(runtime: Runtime) -> None:
    await sync_pending(runtime.engine, settings=runtime.settings, sender=runtime.chat)


async def reminder_job(runtime: Runtime) -> None:
    sent = await run_reminder_pass(runtime.engine, settings=runtime.settings, sender=runtime.chat)
    if sent:
        REMINDERS_SENT.inc(sent)


async def cleanup_job(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    async with sessions.begin() as session:
        await updates.purge_old(
            session, now=now, retention_hours=settings.processed_updates_retention_hours
        )
        await drafts.purge_expired(session, now=now, ttl_hours=settings.draft_ttl_hours)


def start_workers(runtime: Runtime) -> list[asyncio.Task[None]]:
    settings = runtime.settings
    sessions = runtime.sessions
    return [
        asyncio.create_task(
            run_periodic(
                "post_sync", settings.post_sync_poll_seconds, lambda: post_sync_job(runtime)
            )
        ),
        asyncio.create_task(
            run_periodic("reminders", settings.reminder_poll_seconds, lambda: reminder_job(runtime))
        ),
        asyncio.create_task(
            run_periodic(
                "cleanup", CLEANUP_INTERVAL_SECONDS, lambda: cleanup_job(sessions, settings)
            )
        ),
    ]


async def poll_updates(application: Application, runtime: Runtime) -> None:  # type: ignore[type-arg]
    """Long polling. Uses the same dedupe pipeline as the webhook."""
    from support_bot.pipeline import process_update_once

    await application.bot.delete_webhook()
    offset: int | None = None
    while True:
        updates_batch = await application.bot.get_updates(offset=offset, timeout=25)
        for update in updates_batch:
            offset = update.update_id + 1
            try:
                await process_update_once(application, runtime.sessions, runtime.settings, update)
            except Exception:
                ERRORS.labels(component="polling").inc()
                log.exception("polling update failed", component="polling")
