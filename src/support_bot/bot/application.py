from __future__ import annotations

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from support_bot.bot.common import SERVICES_KEY, BotServices
from support_bot.bot.handlers.commands import cmd_cancel, cmd_help, cmd_new, cmd_start
from support_bot.bot.handlers.draft_form import on_draft_callback, on_text
from support_bot.bot.handlers.errors import on_error
from support_bot.bot.handlers.ticket_actions import on_ticket_callback
from support_bot.config import Settings


def build_application(settings: Settings) -> Application:
    application = Application.builder().token(settings.bot_token_value).updater(None).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("new", cmd_new))
    application.add_handler(CommandHandler("cancel", cmd_cancel))
    application.add_handler(CallbackQueryHandler(on_draft_callback, pattern=r"^d:"))
    application.add_handler(CallbackQueryHandler(on_ticket_callback, pattern=r"^t:"))
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
            on_text,
        )
    )
    application.add_error_handler(on_error)
    return application


def attach_services(application: Application, services: BotServices) -> None:
    application.bot_data[SERVICES_KEY] = services
