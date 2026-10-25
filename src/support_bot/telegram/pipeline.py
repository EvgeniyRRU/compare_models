"""Общий конвейер обработки апдейта для webhook и polling.

1. update_id записывается в processed_updates отдельной короткой транзакцией ДО обработки.
2. Если запись уже была — апдейт пропускается (не более одной попытки обработки).
3. Обработка идёт в своих транзакциях; её ошибка или таймаут не откатывают запись (1).
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol

import structlog

from support_bot.monitoring import metrics
from support_bot.monitoring.sentry import capture_exception
from support_bot.services.updates_dedup import UpdateDeduplicator

log = structlog.get_logger(__name__)

type PipelineResult = Literal["processed", "duplicate", "failed", "timeout"]


class HasUpdateId(Protocol):
    @property
    def update_id(self) -> int: ...


class UpdatePipeline:
    def __init__(
        self,
        dedup: UpdateDeduplicator,
        processor: Callable[[Any], Awaitable[None]],
        *,
        timeout: float,
    ) -> None:
        self._dedup = dedup
        self._processor = processor
        self._timeout = timeout

    async def handle(self, update: HasUpdateId) -> PipelineResult:
        user = getattr(update, "effective_user", None)
        with structlog.contextvars.bound_contextvars(
            update_id=update.update_id,
            telegram_user_id=getattr(user, "id", None),
            component="bot",
        ):
            result = await self._handle(update)
        metrics.UPDATES.labels(result=result).inc()
        return result

    async def _handle(self, update: HasUpdateId) -> PipelineResult:
        if not await self._dedup.register(update.update_id):
            log.info("update_duplicate_skipped")
            return "duplicate"
        try:
            async with asyncio.timeout(self._timeout):
                await self._processor(update)
        except TimeoutError as exc:
            log.error("update_timeout", timeout=self._timeout)
            metrics.ERRORS.labels(component="bot").inc()
            capture_exception(exc, component="bot", update_id=update.update_id)
            return "timeout"
        except Exception as exc:
            log.exception("update_failed")
            metrics.ERRORS.labels(component="bot").inc()
            capture_exception(exc, component="bot", update_id=update.update_id)
            return "failed"
        return "processed"
