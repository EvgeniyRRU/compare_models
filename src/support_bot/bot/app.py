from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from telegram.ext import Application, ApplicationBuilder
from telegram.request import BaseRequest

from support_bot.bot.handlers import register_handlers
from support_bot.config import Settings
from support_bot.runtime import Runtime
from support_bot.telegram_adapters import TelegramChat


def build_application(
    settings: Settings,
    engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    *,
    request: BaseRequest | None = None,
) -> Application:  # type: ignore[type-arg]
    builder = ApplicationBuilder().token(settings.bot_token.get_secret_value())
    if request is not None:
        builder = builder.request(request).get_updates_request(request)
    application = builder.build()
    runtime = Runtime(
        settings=settings,
        engine=engine,
        sessions=sessions,
        chat=TelegramChat(bot=application.bot, chat_id=settings.dev_chat_id),
    )
    application.bot_data["runtime"] = runtime
    register_handlers(application)
    return application
