from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import structlog

from support_bot.observability.metrics import observe_error
from support_bot.observability.sentry import capture_exception


class PeriodicRunner:
    """Runs idempotent background loops inside every replica."""

    def __init__(self) -> None:
        self._tasks: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()
        self._logger = structlog.get_logger(__name__)

    def start(
        self,
        name: str,
        interval: float,
        func: Callable[[], Awaitable[object]],
    ) -> None:
        self._tasks.append(asyncio.create_task(self._loop(name, interval, func)))

    async def _loop(
        self,
        name: str,
        interval: float,
        func: Callable[[], Awaitable[object]],
    ) -> None:
        while not self._stop.is_set():
            try:
                await func()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._logger.exception("background task failed", component=name)
                observe_error(name)
                capture_exception(exc, component=name)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except TimeoutError:
                continue

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
