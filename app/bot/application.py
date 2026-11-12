from datetime import UTC, datetime

import sentry_sdk
from sqlalchemy import select
from structlog.contextvars import bind_contextvars, clear_contextvars
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Update,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    TypeHandler,
)

from app.bot.drafts import install_draft_handlers
from app.bot.posts import handle_ticket_callback
from app.core.config import get_settings
from app.db.models import Role, TicketDraft, User
from app.db.session import SessionLocal
from app.services.updates import claim_update


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None or update.effective_message is None:
        return
    telegram_user = update.effective_user
    async with SessionLocal.begin() as session:
        from sqlalchemy import select

        user = await session.scalar(
            select(User).where(User.telegram_id == telegram_user.id).with_for_update()
        )
        if user is None:
            user = User(telegram_id=telegram_user.id)
            session.add(user)
        first_start = user.started_at is None
        user.username = telegram_user.username
        user.full_name = telegram_user.full_name
        if first_start and telegram_user.id in get_settings().bootstrap_ids:
            user.role = Role.ADMIN
        if first_start:
            user.started_at = datetime.now(UTC)
        role = user.role
    if role is None:
        await update.effective_message.reply_text(
            "Ваш аккаунт зарегистрирован. Обратитесь к администратору для назначения роли."
        )
    else:
        chat = update.effective_chat
        is_private = chat is not None and chat.type == "private"
        if chat is not None and is_private:
            await context.bot.set_chat_menu_button(
                chat_id=chat.id,
                menu_button=MenuButtonWebApp(
                    text="Обращения", web_app=WebAppInfo(url=get_settings().miniapp_url)
                ),
            )
        await update.effective_message.reply_text(
            "Бот учёта обращений. Используйте /new для создания обращения или откройте Mini App.",
            reply_markup=(
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "Открыть Mini App",
                                web_app=WebAppInfo(url=get_settings().miniapp_url),
                            )
                        ]
                    ]
                )
                if is_private
                else None
            ),
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(
            "/new — создать обращение\n/cancel — отменить текущую форму"
        )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        if update.effective_user:
            async with SessionLocal.begin() as session:
                user = await session.scalar(
                    select(User).where(User.telegram_id == update.effective_user.id)
                )
                if user:
                    draft = await session.scalar(
                        select(TicketDraft).where(TicketDraft.user_id == user.id).with_for_update()
                    )
                    if draft:
                        await session.delete(draft)
        await update.effective_message.reply_text("Активных форм нет.")


async def polling_deduplication(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        claimed = await claim_update(update.update_id)
    except Exception as error:
        sentry_sdk.capture_exception(error)
        from app.core.metrics import APP_ERRORS

        APP_ERRORS.labels("telegram_deduplication").inc()
        raise ApplicationHandlerStop from error
    if not claimed:
        from app.core.metrics import UPDATES_DUPLICATED

        UPDATES_DUPLICATED.inc()
        raise ApplicationHandlerStop
    from app.core.metrics import UPDATES_PROCESSED

    UPDATES_PROCESSED.inc()


async def bind_update_context(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_contextvars()
    bind_contextvars(
        update_id=update.update_id,
        telegram_user_id=update.effective_user.id if update.effective_user else None,
        component="telegram",
    )
    sentry_sdk.set_tag("update_id", update.update_id)
    if update.effective_user:
        sentry_sdk.set_tag("telegram_user_id", update.effective_user.id)


async def update_user_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None:
        return
    async with SessionLocal.begin() as session:
        user = await session.scalar(
            select(User).where(User.telegram_id == update.effective_user.id).with_for_update()
        )
        if user is None:
            user = User(telegram_id=update.effective_user.id)
            session.add(user)
        user.username = update.effective_user.username
        user.full_name = update.effective_user.full_name


async def telegram_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    from app.core.metrics import APP_ERRORS

    APP_ERRORS.labels("telegram").inc()
    sentry_sdk.capture_exception(context.error)


def make_bot() -> Application:
    application = Application.builder().token(get_settings().bot_token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("cancel", cancel))
    install_draft_handlers(application)
    application.add_handler(CallbackQueryHandler(handle_ticket_callback, pattern=r"^t:"))
    application.add_handler(TypeHandler(Update, bind_update_context), group=-2)
    application.add_handler(TypeHandler(Update, update_user_profile), group=1)
    if get_settings().bot_mode == "polling":
        application.add_handler(TypeHandler(Update, polling_deduplication), group=-1)
    application.add_error_handler(telegram_error)
    return application
