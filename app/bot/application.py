"""PTB Application wiring (no Updater; lifecycle owned by FastAPI lifespan).

Thin adapters only: they unwrap PTB objects into plain data and delegate to the
routed cores, so every behavior lives in testable service/handler functions.
"""

import functools

from telegram import BotCommand, MenuButtonCommands, Message, Update, WebAppInfo
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.bot.handlers.common import Deps
from app.bot.handlers.router import route_callback, route_message
from app.logging_setup import get_logger
from app.sentry import capture_exception

log = get_logger("bot.application")

COMMANDS = [
    BotCommand("new", "Создать обращение"),
    BotCommand("cancel", "Отменить анкету"),
    BotCommand("help", "Справка"),
    BotCommand("start", "Начать"),
]


async def _on_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps: Deps = context.application.bot_data["deps"]
    user, msg = update.effective_user, update.message
    if user is None or msg is None:  # pragma: no cover
        return
    await route_message(
        deps,
        user_id=user.id,
        chat_id=msg.chat_id,
        username=user.username,
        full_name=user.full_name,
        text=msg.text or "",
        reply_to_message_id=None,
    )


async def _on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps: Deps = context.application.bot_data["deps"]
    user, msg = update.effective_user, update.message
    if user is None or msg is None:  # pragma: no cover
        return
    await route_message(
        deps,
        user_id=user.id,
        chat_id=msg.chat_id,
        username=user.username,
        full_name=user.full_name,
        text=msg.text or "",
        reply_to_message_id=msg.reply_to_message.message_id if msg.reply_to_message else None,
    )


async def _on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps: Deps = context.application.bot_data["deps"]
    cb = update.callback_query
    if cb is None or cb.from_user is None:  # pragma: no cover
        return
    await route_callback(
        deps,
        user_id=cb.from_user.id,
        callback_id=cb.id,
        chat_id=cb.message.chat_id if isinstance(cb.message, Message) else cb.from_user.id,
        data=cb.data or "",
    )


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception(
        "ptb handler failed",
        component="bot",
        exc_info=context.error,
    )
    capture_exception(context.error)


async def _post_init(application: Application) -> None:
    """Bot-level registration: command menu + optional Web App menu button."""
    deps: Deps = application.bot_data["deps"]
    bot = application.bot
    await bot.set_my_commands(COMMANDS)
    if deps.settings.web_app_url:
        from telegram import MenuButtonWebApp

        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="Обращения", web_app=WebAppInfo(url=deps.settings.web_app_url)
            )
        )
    else:
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())


def build_application(deps: Deps) -> Application:
    application = (
        Application.builder()
        .token(deps.settings.bot_token)
        .post_init(functools.partial(_post_init))
        .build()
    )
    application.bot_data["deps"] = deps
    application.add_handler(CommandHandler(["start", "help", "cancel", "new"], _on_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_text))
    application.add_handler(CallbackQueryHandler(_on_callback))
    application.add_error_handler(_on_error)
    return application
