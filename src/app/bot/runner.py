import asyncio
from typing import Any

import structlog
from telegram import Update
from telegram.ext import Application

from app import metrics
from app.bot.context import AppContext
from app.logging_setup import bind, clear
from app.sentry_setup import capture
from app.services.updates import claim_update

log = structlog.get_logger(component="updates")


async def process_raw_update(app: Application, ctx: AppContext, data: dict[str, Any]) -> None:  # type: ignore[type-arg]
    """Дедупликация (отдельная короткая транзакция до обработки) + обработка с таймаутом.

    Никогда не бросает исключений: сбой логируется и уходит в Sentry. Не более одной попытки.
    """
    clear()
    update_id = data.get("update_id")
    bind(update_id=update_id, component="updates")
    try:
        if not isinstance(update_id, int):
            return
        if not await claim_update(ctx.sm, update_id):
            metrics.UPDATES.labels("duplicate").inc()
            log.info("update_duplicate")
            return
        update = Update.de_json(data, app.bot)
        await asyncio.wait_for(app.process_update(update), ctx.settings.update_timeout_seconds)
        metrics.UPDATES.labels("processed").inc()
    except Exception as e:
        metrics.UPDATES.labels("failed").inc()
        metrics.ERRORS.labels("update").inc()
        log.exception("update_failed")
        capture(e, component="updates", update_id=update_id)
    finally:
        clear()
