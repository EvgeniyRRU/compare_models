"""Фоновые задачи. Работают в каждой реплике; координация между репликами — через БД
(SKIP LOCKED для напоминаний, pg_try_advisory_lock для синхронизации постов)."""

import asyncio
import random
from collections.abc import Awaitable, Callable

import structlog

from support_bot.container import Container
from support_bot.monitoring import metrics
from support_bot.monitoring.sentry import capture_exception

log = structlog.get_logger(__name__)


async def run_periodic(
    name: str, interval: float, job: Callable[[], Awaitable[object]], *, initial_delay: float = 1.0
) -> None:
    structlog.contextvars.bind_contextvars(component=name)
    await asyncio.sleep(initial_delay * random.random())
    while True:
        try:
            await job()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("worker_iteration_failed")
            metrics.ERRORS.labels(component=name).inc()
            capture_exception(exc, component=name)
        # Небольшой джиттер, чтобы реплики не опрашивали БД синхронно.
        await asyncio.sleep(interval * random.uniform(0.9, 1.1))


def start_workers(container: Container) -> list[asyncio.Task[None]]:
    s = container.settings
    jobs: list[tuple[str, float, Callable[[], Awaitable[object]]]] = [
        ("post_sync", s.post_sync_interval_seconds, container.post_sync.sync_pending),
        ("reminders", s.reminder_poll_interval_seconds, container.reminders.run_once),
        ("cleanup", s.cleanup_interval_seconds, container.cleanup.run_once),
    ]
    return [
        asyncio.create_task(run_periodic(name, interval, job), name=f"worker:{name}")
        for name, interval, job in jobs
    ]


async def stop_tasks(tasks: list[asyncio.Task[None]]) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
