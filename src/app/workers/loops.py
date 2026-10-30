import asyncio
from collections.abc import Awaitable, Callable

import structlog

from app import metrics
from app.bot.context import AppContext
from app.config import Settings
from app.sentry_setup import capture
from app.services import reminders
from app.services.updates import purge_old

log = structlog.get_logger(component="worker")


async def periodic(name: str, interval: float, fn: Callable[[], Awaitable[object]]) -> None:
    """Периодическая задача: ошибки логируются, цикл не падает."""
    while True:
        try:
            await fn()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("worker_failed", worker=name)
            metrics.ERRORS.labels(f"worker_{name}").inc()
            capture(e, component=f"worker_{name}")
        await asyncio.sleep(interval)


async def send_due_reminders(ctx: AppContext) -> int:
    # 1) выбрать и сдвинуть срок в одной транзакции, 2) отправить только после коммита
    async with ctx.sm() as s, s.begin():
        due = await reminders.claim_due(s, ctx.settings.reminder_intervals)
    sent = 0
    for d in due:
        try:  # не более одной попытки на срок
            await ctx.publisher.reply(
                ctx.settings.dev_chat_id,
                d.message_id,
                f"⏰ Обращение #{d.ticket_id} ({d.priority}) до сих пор не взято.",
            )
            metrics.REMINDERS_SENT.labels(d.priority).inc()
            sent += 1
        except Exception as e:
            log.exception("reminder_failed", ticket_id=d.ticket_id)
            metrics.ERRORS.labels("reminder").inc()
            capture(e, component="reminder", ticket_id=d.ticket_id)
    return sent


async def cleanup(ctx: AppContext) -> None:
    async with ctx.sm() as s, s.begin():
        await ctx.drafts.cleanup_expired(s)
        await purge_old(s, _hours(ctx.settings))


def _hours(settings: Settings):  # type: ignore[no-untyped-def]
    from datetime import timedelta

    return timedelta(hours=settings.processed_updates_ttl_hours)


def start_workers(ctx: AppContext) -> list[asyncio.Task[None]]:
    s = ctx.settings
    return [
        asyncio.create_task(
            periodic("post_sync", s.post_sync_poll_seconds, ctx.syncer.sync_pending)
        ),
        asyncio.create_task(
            periodic("reminders", s.reminder_poll_seconds, lambda: send_due_reminders(ctx))
        ),
        asyncio.create_task(periodic("cleanup", s.cleanup_poll_seconds, lambda: cleanup(ctx))),
    ]
