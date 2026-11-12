from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from app.db.models import Priority, Role, TicketDraft, User
from app.db.session import SessionLocal
from app.services.tickets import create_ticket


def draft_callback(action: str, draft_id: UUID, version: int) -> str:
    return f"d:{action}:{draft_id.hex}:{version}"


def parse_draft_callback(value: str) -> tuple[str, UUID, int] | None:
    try:
        prefix, action, raw_id, raw_version = value.split(":")
        if prefix != "d":
            return None
        return action, UUID(hex=raw_id), int(raw_version)
    except ValueError, AttributeError:
        return None


async def save_prompt_if_current(
    session: AsyncSession, *, draft_id: UUID, version: int, step: str, message_id: int
) -> bool:
    result = await session.execute(
        update(TicketDraft)
        .where(TicketDraft.id == draft_id, TicketDraft.version == version, TicketDraft.step == step)
        .values(prompt_message_id=message_id)
    )
    return getattr(result, "rowcount", 0) == 1


async def get_or_register_user(session, telegram_user) -> User:
    user = await session.scalar(
        select(User).where(User.telegram_id == telegram_user.id).with_for_update()
    )
    if user is None:
        user = User(telegram_id=telegram_user.id)
        session.add(user)
    user.username = telegram_user.username
    user.full_name = telegram_user.full_name
    return user


def _prompt_copy(step: str) -> str:
    return {
        "description": "Опишите проблему (до 3000 символов). Ответьте на это сообщение.",
        "request_id": "Введите ID заявки (до 64 символов). Ответьте на это сообщение.",
        "tenant_id": "Введите ID арендатора или нажмите «Пропустить».",
    }[step]


async def show_prompt(message, draft_id: UUID, version: int, step: str, user_id: int) -> None:
    if step == "priority":
        labels = {
            Priority.CRITICAL: "Критический",
            Priority.HIGH: "Высокий",
            Priority.MEDIUM: "Средний",
            Priority.LOW: "Низкий",
        }
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        labels[p],
                        callback_data=draft_callback(f"priority_{p.value}", draft_id, version),
                    )
                ]
                for p in Priority
            ]
        )
        sent = await message.reply_text("Выберите приоритет:", reply_markup=keyboard)
    elif step == "confirm":
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Отправить", callback_data=draft_callback("submit", draft_id, version)
                    ),
                    InlineKeyboardButton(
                        "Отменить", callback_data=draft_callback("cancel", draft_id, version)
                    ),
                ]
            ]
        )
        sent = await message.reply_text("Подтвердите создание обращения:", reply_markup=keyboard)
    else:
        if step == "tenant_id":
            sent = await message.reply_text(
                _prompt_copy(step), reply_markup=ForceReply(selective=True)
            )
            await message.reply_text(
                "ID арендатора можно пропустить:",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "Пропустить",
                                callback_data=draft_callback("skip", draft_id, version),
                            )
                        ]
                    ]
                ),
            )
        else:
            sent = await message.reply_text(
                _prompt_copy(step), reply_markup=ForceReply(selective=True)
            )
    async with SessionLocal.begin() as session:
        await save_prompt_if_current(
            session, draft_id=draft_id, version=version, step=step, message_id=sent.message_id
        )


async def new_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_message or update.effective_chat is None:
        return
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text(
            "Создавать обращения можно только в личном чате с ботом."
        )
        return
    async with SessionLocal.begin() as session:
        user = await get_or_register_user(session, update.effective_user)
        draft = await session.scalar(
            select(TicketDraft).where(TicketDraft.user_id == user.id).with_for_update()
        )
        if user.role not in (Role.SUPPORT, Role.ADMIN):
            step_data = None
        elif draft is None:
            draft = TicketDraft(id=uuid4(), user_id=user.id, step="description", version=1)
            session.add(draft)
            step_data = (draft.id, draft.version, draft.step, False)
        else:
            step_data = (draft.id, draft.version, draft.step, True)
    if user.role not in (Role.SUPPORT, Role.ADMIN):
        await update.effective_message.reply_text(
            "Обратитесь к администратору для назначения роли."
        )
    elif step_data:
        draft_id, version, step, exists = step_data
        if exists:
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Продолжить",
                            callback_data=draft_callback("continue", draft_id, version),
                        ),
                        InlineKeyboardButton(
                            "Начать заново",
                            callback_data=draft_callback("restart", draft_id, version),
                        ),
                    ]
                ]
            )
            await update.effective_message.reply_text(
                "У вас есть незавершённое обращение.", reply_markup=keyboard
            )
        else:
            await show_prompt(
                update.effective_message, draft_id, version, step, update.effective_user.id
            )


async def draft_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if (
        not update.effective_user
        or not update.effective_message
        or update.effective_chat is None
        or update.effective_chat.type != "private"
    ):
        return
    msg = update.effective_message
    raw_text = msg.text
    if raw_text is None:
        return
    async with SessionLocal.begin() as session:
        user = await get_or_register_user(session, update.effective_user)
        draft = await session.scalar(
            select(TicketDraft).where(TicketDraft.user_id == user.id).with_for_update()
        )
        if user.role not in (Role.SUPPORT, Role.ADMIN) or draft is None:
            step_data = None
        elif (
            draft.step not in ("description", "request_id", "tenant_id")
            or not msg.reply_to_message
            or msg.reply_to_message.message_id != draft.prompt_message_id
        ):
            step_data = (draft.id, draft.version, draft.step, "invalid")
        else:
            value = raw_text.strip()
            limit = 3000 if draft.step == "description" else 64
            if not value or len(value) > limit:
                step_data = (draft.id, draft.version, draft.step, "invalid")
            else:
                if draft.step == "description":
                    draft.description, draft.step = value, "priority"
                elif draft.step == "request_id":
                    draft.request_id, draft.step = value, "tenant_id"
                else:
                    draft.tenant_id, draft.step = value, "confirm"
                draft.version += 1
                draft.prompt_message_id = None
                step_data = (draft.id, draft.version, draft.step, "ok")
    if step_data:
        draft_id, version, step, result = step_data
        if result == "invalid":
            await show_prompt(msg, draft_id, version, step, update.effective_user.id)
        else:
            await show_prompt(msg, draft_id, version, step, update.effective_user.id)


async def draft_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user or not query.data:
        return
    parsed = parse_draft_callback(query.data)
    if parsed is None:
        await query.answer("Форма устарела. Введите /new.", show_alert=True)
        return
    action, draft_id, expected_version = parsed
    next_prompt = None
    async with SessionLocal.begin() as session:
        user = await get_or_register_user(session, update.effective_user)
        draft = await session.scalar(
            select(TicketDraft)
            .where(TicketDraft.id == draft_id, TicketDraft.user_id == user.id)
            .with_for_update()
        )
        if user.role not in (Role.SUPPORT, Role.ADMIN):
            outcome = "denied"
        elif draft is None or draft.version != expected_version:
            outcome = "stale"
        elif action == "continue":
            outcome, next_prompt = "ok", (draft.id, draft.version, draft.step)
        elif action == "restart":
            draft.id = uuid4()
            draft.step, draft.description, draft.priority = "description", None, None
            draft.request_id, draft.tenant_id, draft.prompt_message_id = None, None, None
            draft.version += 1
            outcome, next_prompt = "ok", (draft.id, draft.version, draft.step)
        elif action == "cancel":
            await session.delete(draft)
            outcome = "cancelled"
        elif action == "skip" and draft.step == "tenant_id":
            draft.step, draft.prompt_message_id = "confirm", None
            draft.version += 1
            outcome, next_prompt = "ok", (draft.id, draft.version, draft.step)
        elif action.startswith("priority_") and draft.step == "priority":
            try:
                draft.priority = Priority(action.removeprefix("priority_"))
            except ValueError:
                outcome = "stale"
            else:
                draft.step, draft.prompt_message_id = "request_id", None
                draft.version += 1
                outcome, next_prompt = "ok", (draft.id, draft.version, draft.step)
        elif action == "submit" and draft.step == "confirm":
            await create_ticket(
                session,
                author=user,
                description=draft.description or "",
                priority=draft.priority or Priority.MEDIUM,
                request_id=draft.request_id or "",
                tenant_id=draft.tenant_id,
            )
            await session.delete(draft)
            outcome = "submitted"
        else:
            outcome = "stale"
    if outcome == "denied":
        await query.answer("Обратитесь к администратору для назначения роли.", show_alert=True)
    elif outcome == "stale":
        await query.answer("Форма изменилась. Используйте /new.", show_alert=True)
    elif outcome == "cancelled":
        await query.answer("Черновик удалён")
        await query.edit_message_text("Создание обращения отменено.")
    elif outcome == "submitted":
        await query.answer("Обращение создано")
        await query.edit_message_text("Обращение отправлено.")
        from app.bot.posts import sync_outdated

        await sync_outdated(context.bot)
    else:
        await query.answer()
        if next_prompt:
            await show_prompt(query.message, *next_prompt, update.effective_user.id)


def install_draft_handlers(application) -> None:
    application.add_handler(CommandHandler("new", new_ticket))
    application.add_handler(CallbackQueryHandler(draft_callback_handler, pattern=r"^d:"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, draft_text))
