"""PTB factory; HTTP/polling ingestion and lifecycle are owned by main.py."""

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ExtBot,
    MessageHandler,
    TypeHandler,
    filters,
)

from support_bot.config import Settings
from support_bot.services.drafts import DraftService
from support_bot.services.posts import PostService
from support_bot.services.tickets import TicketService
from support_bot.services.users import UserService
from support_bot.telegram.handlers import BotContext, BotHandlers


def build_application(
    settings: Settings,
    users: UserService,
    tickets: TicketService,
    drafts: DraftService,
    posts: PostService,
    *,
    bot: ExtBot | None = None,
) -> Application:
    handlers = BotHandlers(settings, users, tickets, drafts, posts)
    builder = Application.builder().updater(None).context_types(ContextTypes(context=BotContext))
    if bot is None:
        builder = builder.token(settings.bot_token.get_secret_value())
    else:
        builder = builder.bot(bot)
    app = builder.build()
    app.add_handler(TypeHandler(Update, handlers.profile), group=-1)
    for command in ("start", "help", "new", "cancel"):
        app.add_handler(
            CommandHandler(command, getattr(handlers, command), filters.ChatType.PRIVATE)
        )
    app.add_handler(CallbackQueryHandler(handlers.callback))
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handlers.text)
    )
    app.add_error_handler(handlers.error)
    return app
