from datetime import UTC, datetime

import sentry_sdk
import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from support_bot.bot.prompts import question_for
from support_bot.domain.errors import AlreadyTaken, DomainError, StaleTicket
from support_bot.domain.roles import Role
from support_bot.domain.state_machine import Action
from support_bot.metrics import ERRORS, TICKET_TRANSITIONS, TICKETS_CREATED, TIME_TO_TAKE
from support_bot.runtime import Runtime
from support_bot.services import drafts, users
from support_bot.services import tickets as ticket_svc
from support_bot.services.post_render import parse_callback

log = structlog.get_logger()

NO_ACCESS = "Доступа пока нет. Обратитесь к администратору."
ONLY_PRIVATE = "Создавать обращения можно в личке с ботом."
NO_DRAFT = "Черновик не найден. Начните с /new"
HELP_TEXT = (
    "Команды:\n"
    "/new — создать обращение\n"
    "/cancel — отменить черновик\n"
    "/help — справка\n\n"
    "Обращения берутся и ведутся кнопками под постами в чате разработчиков."
)


def _runtime(context: ContextTypes.DEFAULT_TYPE) -> Runtime:
    runtime = context.application.bot_data["runtime"]
    assert isinstance(runtime, Runtime)
    return runtime


async def _actor(runtime: Runtime, update: Update) -> tuple[int, Role | None]:
    """Register or refresh the sender on every interaction and read the role fresh."""
    tg_user = update.effective_user
    assert tg_user is not None
    async with runtime.sessions.begin() as session:
        user = await users.register_user(
            session,
            settings=runtime.settings,
            telegram_id=tg_user.id,
            username=tg_user.username,
            full_name=tg_user.full_name,
        )
        role = await users.current_role(session, user.id)
        return user.id, role


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    runtime = _runtime(context)
    _, role = await _actor(runtime, update)
    assert update.effective_message is not None
    text = NO_ACCESS if role is None else "Готово. /help — список команд."
    await update.effective_message.reply_text(text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.effective_message is not None
    await update.effective_message.reply_text(HELP_TEXT)


async def new_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    runtime = _runtime(context)
    message = update.effective_message
    chat = update.effective_chat
    assert message is not None and chat is not None
    if chat.type != chat.PRIVATE:
        await message.reply_text(ONLY_PRIVATE)
        return
    user_id, role = await _actor(runtime, update)
    if role is None:
        await message.reply_text(NO_ACCESS)
        return
    if not role.can_create:
        await message.reply_text("Создавать обращения могут поддержка и администраторы")
        return

    now = datetime.now(UTC)
    async with runtime.sessions.begin() as session:
        existing = await drafts.get_active(
            session, user_id, now=now, ttl_hours=runtime.settings.draft_ttl_hours
        )
        if existing is None:
            state = await drafts.start(
                session, user_id, now=now, ttl_hours=runtime.settings.draft_ttl_hours
            )
            pending = None
        else:
            state = None
            pending = existing

    if pending is not None:
        await message.reply_text(
            "У вас есть незавершённый черновик.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Продолжить", callback_data="n:c"),
                        InlineKeyboardButton("Начать заново", callback_data="n:r"),
                    ]
                ]
            ),
        )
        return
    assert state is not None
    await _ask(runtime, context, chat.id, state)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    runtime = _runtime(context)
    user_id, _ = await _actor(runtime, update)
    message = update.effective_message
    assert message is not None
    async with runtime.sessions.begin() as session:
        removed = await drafts.cancel(session, user_id)
    await message.reply_text("Черновик отменён" if removed else "Активного черновика нет")


async def new_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    runtime = _runtime(context)
    query = update.callback_query
    assert query is not None and query.message is not None
    user_id, _ = await _actor(runtime, update)
    now = datetime.now(UTC)
    async with runtime.sessions.begin() as session:
        if query.data == "n:r":
            state = await drafts.restart(session, user_id, now=now)
        else:
            state = await drafts.get_active(
                session, user_id, now=now, ttl_hours=runtime.settings.draft_ttl_hours
            ) or await drafts.start(
                session, user_id, now=now, ttl_hours=runtime.settings.draft_ttl_hours
            )
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    await _ask(runtime, context, query.message.chat.id, state)


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    runtime = _runtime(context)
    message = update.effective_message
    chat = update.effective_chat
    assert message is not None and chat is not None
    if chat.type != chat.PRIVATE:
        return
    user_id, _ = await _actor(runtime, update)
    reply_to = message.reply_to_message.message_id if message.reply_to_message else None
    async with runtime.sessions.begin() as session:
        outcome = await drafts.answer_text(
            session,
            user_id=user_id,
            reply_to_message_id=reply_to,
            text=message.text or "",
            now=datetime.now(UTC),
            ttl_hours=runtime.settings.draft_ttl_hours,
        )

    if outcome.kind == "advanced" and outcome.state is not None:
        await _ask(runtime, context, chat.id, outcome.state)
    elif outcome.kind == "invalid" and outcome.state is not None:
        await message.reply_text(outcome.message or "Некорректный ввод")
        await _ask(runtime, context, chat.id, outcome.state)
    elif outcome.kind == "reprompt" and outcome.state is not None:
        await _ask(runtime, context, chat.id, outcome.state)
    else:
        await message.reply_text(outcome.message or NO_DRAFT)


async def draft_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    runtime = _runtime(context)
    query = update.callback_query
    assert query is not None and query.data is not None and query.message is not None
    parsed = drafts.parse_draft_callback(query.data)
    if parsed is None:
        await query.answer()
        return
    code, draft_id, version, value = parsed
    user_id, _ = await _actor(runtime, update)
    try:
        async with runtime.sessions.begin() as session:
            outcome = await drafts.press(
                session,
                user_id=user_id,
                draft_id=draft_id,
                version=version,
                action=drafts.CALLBACK_CODES[code],
                value=value,
                now=datetime.now(UTC),
                ttl_hours=runtime.settings.draft_ttl_hours,
                settings=runtime.settings,
            )
    except DomainError as exc:
        await query.answer(str(exc), show_alert=True)
        return

    chat_id = query.message.chat.id
    if outcome.kind == "created" and outcome.ticket_id is not None:
        TICKETS_CREATED.labels(priority=outcome.priority or "unknown").inc()
        await query.answer("Обращение создано")
        await query.edit_message_text(f"Обращение #{outcome.ticket_id} создано")
        await runtime.sync_post(outcome.ticket_id)
    elif outcome.kind == "cancelled":
        await query.answer()
        await query.edit_message_text("Создание обращения отменено")
    elif outcome.kind == "advanced" and outcome.state is not None:
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=None)
        await _ask(runtime, context, chat_id, outcome.state)
    else:
        await query.answer(outcome.message or "Действие недоступно", show_alert=True)


async def ticket_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    runtime = _runtime(context)
    query = update.callback_query
    assert query is not None and query.data is not None
    parsed = parse_callback(query.data)
    if parsed is None:
        await query.answer()
        return
    action, ticket_id, version = parsed
    user_id, _ = await _actor(runtime, update)
    try:
        async with runtime.sessions.begin() as session:
            ticket = await ticket_svc.transition_at_version(
                session,
                settings=runtime.settings,
                actor_id=user_id,
                ticket_id=ticket_id,
                action=action,
                expected_version=version,
                now=datetime.now(UTC),
            )
            seconds = (
                await ticket_svc.time_to_take_seconds(session, ticket_id)
                if action is Action.TAKE
                else None
            )
    except (StaleTicket, AlreadyTaken) as exc:
        await query.answer(str(exc), show_alert=True)
        await runtime.sync_post(ticket_id)
        return
    except DomainError as exc:
        await query.answer(str(exc), show_alert=True)
        return

    TICKET_TRANSITIONS.labels(action=action.value, to_status=ticket.status).inc()
    if seconds is not None:
        TIME_TO_TAKE.observe(seconds)
    await query.answer()
    await runtime.sync_post(ticket_id)


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    assert query is not None
    data = query.data or ""
    if data.startswith("d:"):
        await draft_callback(update, context)
    elif data.startswith("t:"):
        await ticket_callback(update, context)
    elif data.startswith("n:"):
        await new_choice_callback(update, context)
    else:
        await query.answer()


async def _ask(
    runtime: Runtime,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    state: drafts.DraftState,
) -> None:
    question = question_for(state)
    sent: Message = await context.bot.send_message(
        chat_id=chat_id,
        text=question.text,
        parse_mode=ParseMode.HTML,
        reply_markup=question.markup,
    )
    if question.extra is not None:
        await context.bot.send_message(
            chat_id=chat_id, text="Или пропустите шаг:", reply_markup=question.extra
        )
    async with runtime.sessions.begin() as session:
        await drafts.record_prompt(
            session,
            draft_id=state.draft_id,
            version=state.version,
            step=state.step,
            message_id=sent.message_id,
            now=datetime.now(UTC),
        )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    ERRORS.labels(component="bot").inc()
    error = context.error
    sentry_sdk.set_tag("component", "bot")
    if isinstance(error, TelegramError):
        sentry_sdk.set_tag("telegram_error", type(error).__name__)
    log.error(
        "update handling failed",
        component="bot",
        error_type=type(error).__name__ if error else None,
        exc_info=error,
    )
    if error is not None:
        sentry_sdk.capture_exception(error)


def register_handlers(application: Application) -> None:  # type: ignore[type-arg]
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("new", new_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CallbackQueryHandler(callback_router))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, text_message)
    )
    application.add_error_handler(on_error)
