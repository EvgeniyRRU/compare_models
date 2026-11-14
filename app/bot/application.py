"""python-telegram-bot ``Application`` factory for the bot."""

from typing import Any

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from app.bot.errors import error_handler
from app.bot.handlers.create import (
    cancel_command,
    draft_menu_callback,
    draft_text_answer,
    form_callback,
    new_command,
)
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

    # Draft-form ``/new`` flow (Stage 5e).
    application.add_handler(CommandHandler("new", new_command, filters=filters.ChatType.PRIVATE))
    application.add_handler(
        CommandHandler("cancel", cancel_command, filters=filters.ChatType.PRIVATE)
    )
    application.add_handler(CallbackQueryHandler(draft_menu_callback, pattern="^m:"))
    application.add_handler(CallbackQueryHandler(form_callback, pattern="^f:"))
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, draft_text_answer
        )
    )

    application.add_error_handler(error_handler)

    return application
