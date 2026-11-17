"""PTB Application: дедупликация апдейтов, контекстные теги, Sentry.

PTB 22.8: `process_update(update)` — один аргумент. Ошибки хендлеров не
выходят за пределы process_update (уходит в error handler).
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import sentry_sdk
from structlog.contextvars import bind_contextvars, clear_contextvars
from telegram import BotCommand, Update
from telegram.ext import Application, ApplicationBuilder

from app.config import Settings
from app.db import DB
from app.log import get_logger
from app.metrics import errors_total, telegram_updates_total
from app.services.updates import register_update
from app.services.users import upsert_user

log = get_logger("bot.application")

BOT_COMMANDS = [
    BotCommand("new", "Создать обращение"),
    BotCommand("cancel", "Отменить форму"),
    BotCommand("help", "Справка"),
]


async def _post_init(app: Any) -> None:
    await app.bot.set_my_commands(BOT_COMMANDS)
    if app.settings is not None and app.settings.bot_mode == "webhook":
        await app.bot.set_webhook(
            app.settings.webhook_url, secret_token=app.settings.webhook_secret_token
        )


async def _on_error(bot: Any, context: Any) -> None:
    update = context.update
    update_id = update.update_id if update is not None else None
    user_id = update.effective_user.id if update is not None and update.effective_user else None
    log.error(
        "telegram update error",
        exc_info=context.error,
        update_id=update_id,
        telegram_user_id=user_id,
    )
    errors_total.labels(component="telegram").inc()
    sentry_sdk.capture_exception(context.error)


class AppApplication(Application[Any, Any, Any, Any, Any, Any]):
    @classmethod
    def builder(cls) -> ApplicationBuilder[Any, Any, Any, Any, Any, Any]:
        return ApplicationBuilder().application_class(cls)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.settings: Settings | None = None
        self.db: DB | None = None

    async def process_update(self, update: object) -> None:
        """Дедупликация (отдельная транзакция) + upsert пользователя + теги.

        Работает и в webhook (прямой вызов), и в polling (через update_queue).
        """
        db = self.db
        update_id: int | None = update.update_id if isinstance(update, Update) else None
        if db is not None and update_id is not None:
            fresh = await register_update(db, update_id)
            if not fresh:
                telegram_updates_total.labels(result="duplicate").inc()
                log.debug("duplicate update skipped", update_id=update_id)
                return
            telegram_updates_total.labels(result="processed").inc()
        clear_contextvars()
        effective = update.effective_user if isinstance(update, Update) else None
        bind_contextvars(
            request_id=uuid.uuid4().hex,
            update_id=update_id,
            telegram_user_id=effective.id if effective is not None else None,
        )
        if db is not None and effective is not None:
            # username/имя обновляются при каждом взаимодействии (SPEC «Роли»)
            async with db.session_factory() as session, session.begin():
                await upsert_user(
                    session,
                    telegram_id=effective.id,
                    username=effective.username,
                    first_name=effective.first_name,
                    last_name=effective.last_name,
                )
        try:
            await super().process_update(update)
        finally:
            clear_contextvars()


def build_application(settings: Settings, db: DB, bot: Any = None) -> AppApplication:
    builder = AppApplication.builder()
    builder = builder.bot(bot) if bot is not None else builder.token(settings.bot_token)
    app = cast(AppApplication, builder.post_init(_post_init).build())
    app.settings = settings
    app.db = db
    from app.bot.handlers.callbacks import register_post_callback_handlers  # type: ignore  # noqa: E501
    from app.bot.handlers.commands import register_command_handlers  # type: ignore  # noqa: E501
    from app.bot.handlers.draft_form import register_draft_form_handlers  # type: ignore  # noqa: E501

    register_command_handlers(app)
    register_draft_form_handlers(app)
    register_post_callback_handlers(app)
    app.add_error_handler(_on_error)
    return app
