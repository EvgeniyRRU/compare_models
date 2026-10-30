import contextlib

import structlog
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
    WebAppInfo,
)
from telegram import (
    User as TgUser,
)
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app import metrics
from app.bot.context import AppContext
from app.bot.forms import ask_current, cb
from app.domain.state_machine import (
    Action,
    AlreadyTaken,
    PermissionDenied,
    Role,
    StaleTicket,
    TicketError,
)
from app.logging_setup import bind
from app.sentry_setup import capture
from app.services.drafts import ActionResult, TextResult, decode_id
from app.services.post_render import CODE_ACTIONS
from app.services.tickets import seconds_in_new
from app.services.users import ensure_user

log = structlog.get_logger(component="bot")

HELP = (
    "Бот учёта обращений к дежурному разработчику.\n\n"
    "/new — создать обращение (поддержка, админ)\n"
    "/cancel — отменить создание\n"
    "/help — справка\n\n"
    "Список обращений и управление — в Mini App (кнопка меню)."
)
NO_ROLE = "У вас пока нет доступа. Обратитесь к администратору, чтобы получить роль."
STALE_FORM = "Форма устарела, продолжаем с актуального шага."


def app_ctx(context: ContextTypes.DEFAULT_TYPE) -> AppContext:
    ctx: AppContext = context.application.bot_data["ctx"]
    return ctx


async def _register(ctx: AppContext, tg: TgUser):  # type: ignore[no-untyped-def]
    bind(telegram_user_id=tg.id)
    async with ctx.sm() as s, s.begin():
        return await ensure_user(
            s, tg.id, tg.username, tg.first_name, tg.last_name, ctx.settings.bootstrap_admin_ids
        )


def _private(update: Update) -> bool:
    return update.effective_chat is not None and update.effective_chat.type == ChatType.PRIVATE


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = app_ctx(context)
    if update.effective_user is None or update.message is None:
        return
    user = await _register(ctx, update.effective_user)
    if user.role is None:
        await update.message.reply_text(NO_ROLE)
        return
    markup = None
    if ctx.settings.webapp_url and _private(update):
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Открыть Mini App", web_app=WebAppInfo(ctx.settings.webapp_url)
                    )
                ]
            ]
        )
    await update.message.reply_text(
        f"Привет! Ваша роль: {user.role}.\n\n{HELP}", reply_markup=markup
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None or update.message is None:
        return
    await _register(app_ctx(context), update.effective_user)
    await update.message.reply_text(HELP)


async def new_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = app_ctx(context)
    msg, tg = update.message, update.effective_user
    if msg is None or tg is None:
        return
    user = await _register(ctx, tg)
    if not _private(update):
        await msg.reply_text("Создавать обращения можно только в личке с ботом.")
        return
    if user.role not in (Role.SUPPORT.value, Role.ADMIN.value):
        await msg.reply_text(
            NO_ROLE if user.role is None else "Создавать обращения могут поддержка и админы."
        )
        return
    async with ctx.sm() as s, s.begin():
        existing = await ctx.drafts.get_active(s, user.id)
        draft = existing or await ctx.drafts.start(s, user.id)
    if existing:
        await msg.reply_text(
            "У вас есть незавершённое обращение.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Продолжить", callback_data=cb("c", existing)),
                        InlineKeyboardButton("Начать заново", callback_data=cb("r", existing)),
                    ]
                ]
            ),
        )
        return
    await ask_current(ctx, context.bot, msg.chat_id, draft, user.id)


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = app_ctx(context)
    if update.effective_user is None or update.message is None:
        return
    user = await _register(ctx, update.effective_user)
    async with ctx.sm() as s, s.begin():
        had = await ctx.drafts.cancel(s, user.id)
    await update.message.reply_text("Создание обращения отменено." if had else "Нечего отменять.")


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = app_ctx(context)
    msg, tg = update.message, update.effective_user
    if msg is None or tg is None or not msg.text:
        return
    user = await _register(ctx, tg)
    reply_to = msg.reply_to_message.message_id if msg.reply_to_message else None
    async with ctx.sm() as s, s.begin():
        result, draft, error = await ctx.drafts.submit_text(s, user.id, msg.text, reply_to)
    if result == TextResult.NO_DRAFT:
        if user.role is not None:
            await msg.reply_text("Чтобы создать обращение, отправьте /new.")
        return
    assert draft is not None
    if result == TextResult.INVALID:
        await msg.reply_text(error or "Некорректное значение.")
    # ACCEPTED: следующий вопрос; UNBOUND/INVALID: заново текущий вопрос
    await ask_current(ctx, context.bot, msg.chat_id, draft, user.id)


async def form_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = app_ctx(context)
    q = update.callback_query
    if q is None or q.data is None:
        return
    user = await _register(ctx, q.from_user)
    parts = q.data.split(":")
    draft_id = decode_id(parts[2]) if len(parts) >= 4 else None
    if draft_id is None or not parts[3].isdigit():
        await q.answer(STALE_FORM, show_alert=True)
        return
    action, version = parts[1], int(parts[3])
    value = parts[4] if len(parts) > 4 else None
    chat_id = q.message.chat.id if q.message else q.from_user.id
    try:
        async with ctx.sm() as s, s.begin():
            result, draft, ticket = await ctx.drafts.press(
                s, user.id, draft_id, version, action, value
            )
    except PermissionDenied as e:
        await q.answer(str(e), show_alert=True)
        return
    if result == ActionResult.STALE:
        await q.answer(STALE_FORM, show_alert=True)
        if draft:
            await ask_current(ctx, context.bot, chat_id, draft, user.id)
        return
    await q.answer()
    await _clear_markup(q)
    if result == ActionResult.CANCELLED:
        await context.bot.send_message(
            chat_id, "Создание обращения отменено.", reply_markup=ReplyKeyboardRemove()
        )
    elif result == ActionResult.SUBMITTED:
        assert ticket is not None
        metrics.TICKETS_CREATED.labels(ticket.priority).inc()
        bind(ticket_id=ticket.id)
        await context.bot.send_message(chat_id, f"Обращение #{ticket.id} создано.")
        await sync_post_safe(ctx, ticket.id)
    elif draft:
        await ask_current(ctx, context.bot, chat_id, draft, user.id)


async def _clear_markup(q) -> None:  # type: ignore[no-untyped-def]
    with contextlib.suppress(Exception):
        await q.edit_message_reply_markup(reply_markup=None)


async def sync_post_safe(ctx: AppContext, ticket_id: int) -> None:
    """Синхронизация после коммита; сбой подберёт фоновая задача."""
    try:
        await ctx.syncer.sync(ticket_id)
    except Exception as e:
        log.exception("post_sync_failed", ticket_id=ticket_id)
        metrics.ERRORS.labels("post_sync").inc()
        capture(e, component="post_sync", ticket_id=ticket_id)


async def ticket_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопки поста: t:<код>:<id>:<version>. Права проверяются на сервере."""
    ctx = app_ctx(context)
    q = update.callback_query
    if q is None or q.data is None:
        return
    parts = q.data.split(":")
    action = CODE_ACTIONS.get(parts[1]) if len(parts) == 4 else None
    if action is None or not parts[2].isdigit() or not parts[3].isdigit():
        await q.answer("Неизвестное действие", show_alert=True)
        return
    ticket_id, version = int(parts[2]), int(parts[3])
    bind(ticket_id=ticket_id)
    user = await _register(ctx, q.from_user)
    try:
        async with ctx.sm() as s, s.begin():
            ticket = await ctx.tickets.apply(s, ticket_id, action, user.id, version)
            elapsed = await seconds_in_new(s, ticket_id) if action == Action.TAKE else None
    except (AlreadyTaken, StaleTicket) as e:
        await q.answer(str(e), show_alert=True)
        await sync_post_safe(ctx, ticket_id)  # пересинхронизация поста
        return
    except TicketError as e:
        await q.answer(str(e), show_alert=True)
        return
    metrics.TICKET_TRANSITIONS.labels(action.value, ticket.status).inc()
    if elapsed is not None:
        metrics.TIME_TO_TAKE.observe(elapsed)
    await q.answer()
    await sync_post_safe(ctx, ticket_id)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    log.error("handler_error", exc_info=err)
    metrics.ERRORS.labels("bot_handler").inc()
    if err is not None:
        user_id = (
            update.effective_user.id
            if isinstance(update, Update) and update.effective_user
            else None
        )
        capture(err, component="bot_handler", telegram_user_id=user_id)


def register(app: Application) -> None:  # type: ignore[type-arg]
    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("new", new_cmd, filters=private))
    app.add_handler(CommandHandler("cancel", cancel_cmd, filters=private))
    app.add_handler(CallbackQueryHandler(form_callback, pattern=r"^d:"))
    app.add_handler(CallbackQueryHandler(ticket_callback, pattern=r"^t:"))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, text_message))
    app.add_error_handler(error_handler)
