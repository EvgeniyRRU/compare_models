from __future__ import annotations

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from support_bot.observability.metrics import observe_error
from support_bot.observability.sentry import capture_exception

logger = structlog.get_logger(__name__)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    logger.exception("bot handler failed", component="bot", error=repr(error))
    observe_error("bot")
    if isinstance(error, BaseException):
        tags: dict[str, object] = {"component": "bot"}
        if isinstance(update, Update) and update.update_id is not None:
            tags["update_id"] = update.update_id
        capture_exception(error, **tags)
