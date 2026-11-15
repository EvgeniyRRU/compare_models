"""PTB error handler: logs unhandled exceptions and reports them to Sentry."""

import logging

import sentry_sdk
from structlog.contextvars import get_contextvars
from telegram.ext import ContextTypes

from app.metrics import errors_total

logger = logging.getLogger(__name__)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """PTB error handler: log the exception and send it to Sentry."""
    logger.error("Unhandled PTB error", exc_info=context.error)
    tags = {"component": "bot", **get_contextvars()}
    sentry_sdk.capture_exception(context.error, tags=tags)
    errors_total.labels(component="bot").inc()
