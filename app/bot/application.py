"""python-telegram-bot ``Application`` factory for the bot."""

from typing import Any

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    TypeHandler,
)

from app.bot.errors import error_handler
from app.bot.handlers.start import help_command, start
from app.bot.handlers.ticket_actions import ticket_action_callback
from app.bot.middleware import register_user_middleware
from app.config import settings


def build_application() -> Application[Any, Any, Any, Any, Any, Any]:
    """Build the PTB ``Application`` (no Updater / ``run_polling`` here).

    The FastAPI lifespan calls ``initialize()``/``process_update()`` later. The
    draft-form ``/new``/``/cancel`` handlers (Stage 5e) slot in as additional
    ``CommandHandler``/``CallbackQueryHandler`` registrations below.
    """
    application = ApplicationBuilder().token(settings.bot_token).build()

    # User-identification middleware: runs before every other handler (group -1).
    # It returns ``None``, so dispatch continues to the actual handler.
    application.add_handler(TypeHandler(Update, register_user_middleware), group=-1)

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(ticket_action_callback, pattern="^t:"))

    application.add_error_handler(error_handler)

    return application
