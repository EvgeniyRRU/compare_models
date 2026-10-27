import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from structlog.contextvars import bound_contextvars

from support_bot.config import Settings
from support_bot.db.base import Database
from support_bot.db.models import TicketDraft
from support_bot.observability.sentry import capture_error
from support_bot.services.posts import PostService
from support_bot.services.reminders import ReminderService
from support_bot.services.updates import UpdateService


async def cleanup(db: Database, updates: UpdateService, settings: Settings) -> None:
    while await updates.cleanup() == settings.worker_batch_size:  # noqa: ASYNC110
        await asyncio.sleep(0)
    while True:
        async with db.sessions.begin() as session:
            expired = (
                select(TicketDraft.id)
                .where(
                    TicketDraft.updated_at
                    < datetime.now(UTC) - timedelta(hours=settings.draft_ttl_hours)
                )
                .limit(settings.worker_batch_size)
                .with_for_update(skip_locked=True)
            )
            deleted = await session.scalars(
                delete(TicketDraft).where(TicketDraft.id.in_(expired)).returning(TicketDraft.id)
            )
            count = len(deleted.all())
        if count < settings.worker_batch_size:
            return
        await asyncio.sleep(0)


async def periodic(name: str, interval: float, operation: Callable[[], Awaitable[None]]) -> None:
    with bound_contextvars(component=name):
        while True:
            try:
                await operation()
            except Exception as error:
                capture_error(error, component=name)
            await asyncio.sleep(interval)


def start_workers(
    db: Database,
    posts: PostService,
    reminders: ReminderService,
    updates: UpdateService,
    settings: Settings,
) -> list[asyncio.Task[None]]:
    async def clean() -> None:
        await cleanup(db, updates, settings)

    return [
        asyncio.create_task(
            periodic("post_sync", settings.post_sync_poll_seconds, posts.reconcile),
            name="post_sync",
        ),
        asyncio.create_task(
            periodic("reminders", settings.reminder_poll_seconds, reminders.run_once),
            name="reminders",
        ),
        asyncio.create_task(
            periodic("cleanup", settings.cleanup_poll_seconds, clean), name="cleanup"
        ),
    ]


async def stop_workers(tasks: list[asyncio.Task[None]]) -> None:
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
