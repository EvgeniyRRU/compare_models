"""PTB error handler: logs unhandled exceptions (Sentry is wired in Stage 8)."""

import logging

from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


async def error_handler(update: object | None, context: ContextTypes.DEFAULT_TYPE) -> None:
    """PTB error handler: log the exception. Sentry is wired in Stage 8."""
    logger.error("Unhandled PTB error", exc_info=context.error)
