import base64
import logging
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert
from structlog.contextvars import bind_contextvars
from telegram import (
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Update,
    WebAppInfo,
)
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler, TypeHandler, filters

from app.db.models import Ticket, TicketDraft, User
from app.domain.state_machine import InvalidTransition
from app.services.drafts import (
    advance_button,
    advance_text,
    cancel_draft,
    draft_for_user,
    new_draft,
    save_prompt,
)
from app.services.posts import sync_post
from app.services.tickets import Conflict, Forbidden, transition_ticket

log = logging.getLogger(__name__)


def short_id(value: UUID) -> str:
    return base64.urlsafe_b64encode(value.bytes).decode().rstrip("=")


def decode_id(value: str) -> UUID:
    return UUID(bytes=base64.urlsafe_b64decode(value + "=="))


async def identity(update, context):
    person = update.effective_user
    if person is None:
        return None
    sessions = context.application.bot_data["sessions"]
    settings = context.application.bot_data["settings"]
    name = person.full_name[:255]
    text = (update.message.text or "").strip() if update.message else ""
    is_start = bool(text and text.split()[0].split("@")[0] == "/start")
    bootstrap = is_start and person.id in settings.bootstrap_admin_ids
    async with sessions.begin() as session:
        stmt = insert(User).values(
            telegram_id=person.id,
            username=person.username,
            name=name,
            role="admin" if bootstrap else None,
            started=is_start,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[User.telegram_id],
            set_={
                "username": person.username,
                "name": name,
                "updated_at": func.now(),
                "started": User.started | is_start,
                "role": case((~User.started & bootstrap, "admin"), else_=User.role),
            },
        )
        await session.execute(stmt)
        user = await session.scalar(select(User).where(User.telegram_id == person.id))
        return user


async def start(update, context):
    user = await identity(update, context)
    if not user:
        return
    if not user.role:
        await update.effective_message.reply_text(
            "Обратитесь к администратору для получения доступа."
        )
        return
    if update.effective_chat.type == "private":
        settings = context.application.bot_data["settings"]
        await context.bot.set_chat_menu_button(
            chat_id=update.effective_chat.id,
            menu_button=MenuButtonWebApp(
                text="Обращения", web_app=WebAppInfo(url=settings.mini_app_url)
            ),
        )
    await update.effective_message.reply_text(
        "Для создания обращения используйте /new. Отмена: /cancel."
    )


async def show_prompt(context, chat_id, draft):
    prompts = {
        "description": "Опишите обращение (до 3000 символов).",
        "priority": "Выберите приоритет.",
        "request_id": "Укажите ID заявки (до 64 символов).",
        "tenant_id": "Укажите ID арендатора (до 64 символов) или пропустите.",
        "confirm": f"Отправить обращение?\n\nОписание: {draft.description}\nПриоритет: {draft.priority}\nЗаявка: {draft.request_id}\nАрендатор: {draft.tenant_id or '—'}",
    }
    markup: ForceReply | InlineKeyboardMarkup | None = None
    if draft.step in {"description", "request_id", "tenant_id"}:
        markup = ForceReply()
    if draft.step == "priority":
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        x, callback_data=f"d:{short_id(draft.id)}:{draft.version}:{x}"
                    )
                    for x in ("critical", "high", "medium", "low")
                ]
            ]
        )
    if draft.step == "confirm":
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Отправить", callback_data=f"d:{short_id(draft.id)}:{draft.version}:submit"
                    ),
                    InlineKeyboardButton(
                        "Отменить", callback_data=f"d:{short_id(draft.id)}:{draft.version}:cancel"
                    ),
                ]
            ]
        )
    msg = await context.bot.send_message(
        chat_id=chat_id, text=prompts[draft.step], reply_markup=markup
    )
    if draft.step in {"description", "request_id", "tenant_id"}:
        async with context.application.bot_data["sessions"].begin() as session:
            await save_prompt(session, draft.id, draft.version, draft.step, msg.message_id)
    if draft.step == "tenant_id":
        await context.bot.send_message(
            chat_id=chat_id,
            text="Или пропустите этот шаг:",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Пропустить",
                            callback_data=f"d:{short_id(draft.id)}:{draft.version}:skip",
                        )
                    ]
                ]
            ),
        )


async def new(update, context):
    user = await identity(update, context)
    if not user or user.role not in {"support", "admin"} or update.effective_chat.type != "private":
        await update.effective_message.reply_text("Нет доступа.")
        return
    try:
        async with context.application.bot_data["sessions"].begin() as session:
            locked_user = await session.scalar(
                select(User).where(User.id == user.id).with_for_update()
            )
            if locked_user is None or locked_user.role not in {"support", "admin"}:
                raise Forbidden()
            existing = await draft_for_user(session, user.id, lock=True)
            if existing:
                draft = existing
            else:
                draft = await new_draft(session, user.id)
    except Forbidden:
        await update.effective_message.reply_text("Нет доступа.")
        return
    if existing:
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Продолжить",
                        callback_data=f"d:{short_id(draft.id)}:{draft.version}:continue",
                    ),
                    InlineKeyboardButton(
                        "Начать заново",
                        callback_data=f"d:{short_id(draft.id)}:{draft.version}:restart",
                    ),
                ]
            ]
        )
        await update.effective_message.reply_text("У вас есть черновик.", reply_markup=markup)
        return
    await show_prompt(context, update.effective_chat.id, draft)


async def cancel(update, context):
    user = await identity(update, context)
    if user and update.effective_chat.type == "private":
        async with context.application.bot_data["sessions"].begin() as session:
            await cancel_draft(session, user.id)
        await update.effective_message.reply_text("Черновик удалён.")


async def text_reply(update, context):
    user = await identity(update, context)
    if not user or user.role not in {"support", "admin"} or update.effective_chat.type != "private":
        return
    sessions = context.application.bot_data["sessions"]
    draft: TicketDraft | None
    async with sessions.begin() as session:
        try:
            reply = update.message.reply_to_message
            draft = await advance_text(
                session, user.id, reply.message_id if reply else None, update.message.text or ""
            )
        except Conflict, ValueError:
            draft = await draft_for_user(session, user.id)
        except LookupError, Forbidden:
            return
    if draft:
        await show_prompt(context, update.effective_chat.id, draft)


async def callback(update, context):
    query = update.callback_query
    user = await identity(update, context)
    if not user or not user.role:
        await query.answer("Нет доступа", show_alert=True)
        return
    parts = (query.data or "").split(":")
    sessions = context.application.bot_data["sessions"]
    try:
        if len(parts) == 4 and parts[0] == "t":
            action, ticket_id, version = parts[1], int(parts[2]), int(parts[3])
            bind_contextvars(ticket_id=ticket_id)
            mapping = {
                "take": ("new", "in_progress"),
                "wait": ("in_progress", "waiting_info"),
                "work": ("waiting_info", "in_progress"),
            }
            async with sessions.begin() as session:
                ticket = await session.get(Ticket, ticket_id)
                if ticket is None:
                    raise Conflict()
                if action in mapping:
                    expected, target = mapping[action]
                else:
                    expected = ticket.status
                    target = {
                        "resolve": "resolved",
                        "reject": "rejected",
                        "dup": "duplicate",
                        "release": "new",
                    }[action]
                await transition_ticket(
                    session,
                    ticket_id,
                    user.id,
                    expected,
                    version,
                    target,
                    context.application.bot_data["intervals"],
                )
            await query.answer()
            await sync_post(
                context.application.bot_data["engine"],
                context.bot,
                context.application.bot_data["settings"].dev_chat_id,
                ticket_id,
            )
        elif (
            len(parts) == 4
            and parts[0] == "d"
            and update.effective_chat.type == "private"
            and user.role in {"support", "admin"}
        ):
            draft_id, version, action = decode_id(parts[1]), int(parts[2]), parts[3]
            async with sessions.begin() as session:
                locked_user = await session.scalar(
                    select(User).where(User.id == user.id).with_for_update()
                )
                if not locked_user or locked_user.role not in {"support", "admin"}:
                    raise Forbidden()
                draft = await draft_for_user(session, user.id, lock=True)
                if draft is None or draft.id != draft_id or draft.version != version:
                    raise Conflict()
                if action == "cancel":
                    await session.delete(draft)
                    result = None
                elif action == "restart":
                    result = await new_draft(session, user.id, restart=True)
                elif action == "continue":
                    result = draft
                else:
                    result = await advance_button(
                        session,
                        user.id,
                        draft_id,
                        version,
                        action,
                        context.application.bot_data["intervals"],
                    )
            await query.answer()
            if isinstance(result, TicketDraft):
                await show_prompt(context, update.effective_chat.id, result)
            elif result is not None:
                await query.message.reply_text(f"Обращение #{result.id} создано.")
                await sync_post(
                    context.application.bot_data["engine"],
                    context.bot,
                    context.application.bot_data["settings"].dev_chat_id,
                    result.id,
                )
            else:
                await query.message.reply_text("Черновик удалён.")
        else:
            await query.answer("Некорректное действие", show_alert=True)
    except (Conflict, Forbidden, InvalidTransition, ValueError, KeyError) as exc:
        if isinstance(exc, Conflict) and len(parts) == 4 and parts[0:2] == ["t", "take"]:
            async with sessions() as session:
                current = await session.get(Ticket, int(parts[2]))
            message = "Уже взято" if current and current.status != "new" else "Обращение изменилось"
        elif isinstance(exc, Conflict):
            message = (
                "Обращение изменилось"
                if parts[0] == "t"
                else "Черновик изменился. Продолжите с актуального шага."
            )
        else:
            message = "Нет доступа или недопустимое действие"
        await query.answer(message, show_alert=True)
        if parts and parts[0] == "t":
            try:
                await sync_post(
                    context.application.bot_data["engine"],
                    context.bot,
                    context.application.bot_data["settings"].dev_chat_id,
                    int(parts[2]),
                )
            except Exception:
                log.exception("Failed to sync after conflict")


def register(application):
    application.add_handler(TypeHandler(Update, identity), group=-1)
    application.add_handler(CommandHandler(["start", "help"], start))
    application.add_handler(CommandHandler("new", new))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CallbackQueryHandler(callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_reply))
