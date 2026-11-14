"""``/new`` multi-step draft form handlers (description → priority → request_id →
tenant_id → confirm) plus ``/cancel``.

The form is *not* a ``ConversationHandler``: draft state lives in the
``ticket_drafts`` table (one active draft per user) so neighbouring updates can
safely land on different replicas. Every accepted step transition is a single
guarded ``UPDATE`` in the service layer, committed here *before* the next
question is sent; the question's ``message_id`` is then recorded with a separate
short ``set_prompt_message_id`` transaction so the DB transaction is never held
open across a Telegram send.

Text answers are bound to their question via ``ForceReply``: an answer is only
accepted when ``message.reply_to_message.message_id == draft.prompt_message_id``.
Anything else (a non-reply, a reply to a stale question, or a message that isn't
bound) re-asks the current step without mutating data, step, or version.
"""

import logging
from typing import Any, cast

from telegram import Chat, ForceReply, Update
from telegram.ext import ContextTypes

from app.bot.callbacks import decode_draft_menu_callback, decode_form_callback
from app.bot.keyboards import (
    confirm_keyboard,
    draft_menu_keyboard,
    priority_keyboard,
    tenant_id_keyboard,
)
from app.bot.post_client import BotPostClient
from app.config import settings
from app.db.enums import DraftStep, Role
from app.db.models import TicketDraft
from app.db.session import async_session_factory, engine
from app.services.draft_service import (
    DraftChanged,
    DraftNotFound,
    DraftValidationError,
    confirm,
    create_draft,
    delete_draft,
    get_active_draft,
    get_draft,
    save_description,
    save_request_id,
    save_tenant_id,
    set_priority,
    set_prompt_message_id,
)
from app.services.post_sync import sync_ticket
from app.services.render import PRIORITY_LABELS
from app.services.ticket_service import PermissionDenied

logger = logging.getLogger(__name__)

_DESCRIPTION_PROMPT = "Опишите проблему (до 3000 символов):"
_PRIORITY_PROMPT = "Выберите приоритет:"
_REQUEST_ID_PROMPT = "Укажите ID заявки (до 64 символов):"
_TENANT_ID_PROMPT = "Укажите ID арендатора (до 64 символов):"
_TENANT_ID_SKIP_PROMPT = "Или пропустите этот шаг:"


def _render_confirm_summary(draft: TicketDraft) -> str:
    """Render the confirmation summary shown before «Отправить / Отменить»."""
    priority_label = PRIORITY_LABELS[draft.priority] if draft.priority is not None else "—"
    lines = [
        "Проверьте данные обращения:",
        "",
        f"Приоритет: {priority_label}",
        f"Заявка: {draft.request_id or ''}",
    ]
    if draft.tenant_id:
        lines.append(f"Арендатор: {draft.tenant_id}")
    lines += ["", "Описание:", draft.description or ""]
    return "\n".join(lines)


async def _send_force_reply_question(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    draft: TicketDraft,
    text: str,
) -> None:
    """Send a ``ForceReply`` question and record its ``message_id`` (technical write).

    The send happens before any transaction opens; ``set_prompt_message_id`` then
    runs in its own short transaction, guarded on the current version+step, so a
    late prompt-send can never overwrite the next step's ``prompt_message_id``.
    """
    sent = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=ForceReply())
    async with async_session_factory() as session:
        await set_prompt_message_id(
            session,
            draft_id=draft.id,
            expected_version=draft.version,
            expected_step=draft.step,
            message_id=sent.message_id,
        )
        await session.commit()


async def _send_step_prompt(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    draft: TicketDraft,
) -> None:
    """Send the prompt for ``draft.step`` (recording ``prompt_message_id`` for text steps)."""
    if draft.step is DraftStep.description:
        await _send_force_reply_question(context, chat_id, draft, _DESCRIPTION_PROMPT)
    elif draft.step is DraftStep.priority:
        await context.bot.send_message(
            chat_id=chat_id, text=_PRIORITY_PROMPT, reply_markup=priority_keyboard(draft)
        )
    elif draft.step is DraftStep.request_id:
        await _send_force_reply_question(context, chat_id, draft, _REQUEST_ID_PROMPT)
    elif draft.step is DraftStep.tenant_id:
        await _send_force_reply_question(context, chat_id, draft, _TENANT_ID_PROMPT)
        # The skip button can't share a message with ``ForceReply``, so it gets
        # its own short keyboard message.
        await context.bot.send_message(
            chat_id=chat_id,
            text=_TENANT_ID_SKIP_PROMPT,
            reply_markup=tenant_id_keyboard(draft),
        )
    elif draft.step is DraftStep.confirm:
        await context.bot.send_message(
            chat_id=chat_id,
            text=_render_confirm_summary(draft),
            reply_markup=confirm_keyboard(draft),
        )


async def new_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/new``: resume an existing draft or start a fresh one."""
    message = update.effective_message
    if message is None:
        return
    chat = update.effective_chat
    if chat is None or chat.type != Chat.PRIVATE:
        return

    user = cast(dict[str, Any], context.user_data).get("user")
    if user is None:
        return
    if user.role not in (Role.support, Role.admin):
        await message.reply_text("Обратитесь к администратору для доступа к боту")
        return

    draft: TicketDraft | None = None
    existing: TicketDraft | None = None
    async with async_session_factory() as session:
        existing = await get_active_draft(session, user.id)
        if existing is None:
            draft = await create_draft(session, user_id=user.id)
            await session.commit()

    if existing is not None:
        await message.reply_text(
            "У вас есть незавершённое обращение.",
            reply_markup=draft_menu_keyboard(existing),
        )
        return

    assert draft is not None
    await _send_step_prompt(context, chat.id, draft)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/cancel``: discard the user's active draft, if any."""
    message = update.effective_message
    if message is None:
        return
    chat = update.effective_chat
    if chat is None or chat.type != Chat.PRIVATE:
        return

    user = cast(dict[str, Any], context.user_data).get("user")
    if user is None:
        return

    deleted = False
    async with async_session_factory() as session:
        draft = await get_active_draft(session, user.id)
        if draft is not None:
            await delete_draft(session, draft_id=draft.id)
            deleted = True
            await session.commit()

    await message.reply_text("Создание отменено." if deleted else "Нет активного черновика.")


async def draft_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the ``/new`` continue/restart menu buttons (``m:...``)."""
    query = update.callback_query
    if query is None:
        return

    answered = False

    async def answer(text: str | None = None, *, show_alert: bool | None = None) -> None:
        nonlocal answered
        if answered:
            return
        answered = True
        await query.answer(text=text, show_alert=show_alert)

    chat = update.effective_chat
    if chat is None or chat.type != Chat.PRIVATE:
        await answer()
        return

    user = cast(dict[str, Any], context.user_data).get("user")
    if user is None:
        await answer("Пользователь не найден", show_alert=True)
        return

    try:
        decoded = decode_draft_menu_callback(query.data or "")
        if decoded is None:
            await answer("Неверные данные кнопки", show_alert=True)
            return
        action, draft_id, _version = decoded

        draft: TicketDraft | None = None
        async with async_session_factory() as session:
            existing = await get_draft(session, draft_id)
            if action == "restart":
                if existing is not None and existing.user_id != user.id:
                    await answer("Черновик не найден", show_alert=True)
                    return
                await delete_draft(session, draft_id=draft_id)
                # Clear any other active draft (stale-menu race) so the fresh
                # INSERT can't hit the UNIQUE(user_id) constraint, then start over.
                active = await get_active_draft(session, user.id)
                if active is not None:
                    await delete_draft(session, draft_id=active.id)
                draft = await create_draft(session, user_id=user.id)
                await session.commit()
            else:  # "continue"
                if existing is None or existing.user_id != user.id:
                    await answer("Черновик не найден", show_alert=True)
                    return
                draft = existing

        assert draft is not None
        await _send_step_prompt(context, chat.id, draft)
        await answer()
    finally:
        await answer()


async def form_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a draft-form inline button (``f:...``): priority / skip / ok / no."""
    query = update.callback_query
    if query is None:
        return

    answered = False

    async def answer(text: str | None = None, *, show_alert: bool | None = None) -> None:
        nonlocal answered
        if answered:
            return
        answered = True
        await query.answer(text=text, show_alert=show_alert)

    chat = update.effective_chat
    if chat is None or chat.type != Chat.PRIVATE:
        await answer()
        return

    user = cast(dict[str, Any], context.user_data).get("user")
    if user is None:
        await answer("Пользователь не найден", show_alert=True)
        return

    try:
        decoded = decode_form_callback(query.data or "")
        if decoded is None:
            await answer("Неверные данные кнопки", show_alert=True)
            return

        try:
            if decoded.action == "priority":
                priority = decoded.priority
                if priority is None:
                    await answer("Неверные данные кнопки", show_alert=True)
                    return
                async with async_session_factory() as session:
                    existing = await get_draft(session, decoded.draft_id)
                    if existing is None or existing.user_id != user.id:
                        await answer("Черновик не найден", show_alert=True)
                        return
                    draft = await set_priority(
                        session,
                        draft_id=decoded.draft_id,
                        expected_version=decoded.version,
                        priority=priority,
                    )
                    await session.commit()
                await _send_step_prompt(context, chat.id, draft)
            elif decoded.action == "skip":
                async with async_session_factory() as session:
                    existing = await get_draft(session, decoded.draft_id)
                    if existing is None or existing.user_id != user.id:
                        await answer("Черновик не найден", show_alert=True)
                        return
                    draft = await save_tenant_id(
                        session,
                        draft_id=decoded.draft_id,
                        expected_version=decoded.version,
                        tenant_id=None,
                    )
                    await session.commit()
                await _send_step_prompt(context, chat.id, draft)
            elif decoded.action == "ok":
                async with async_session_factory() as session:
                    ticket = await confirm(
                        session,
                        draft_id=decoded.draft_id,
                        expected_version=decoded.version,
                        author=user,
                    )
                    await session.commit()
                await answer("Обращение создано")
                client = BotPostClient(context.bot, settings.dev_chat_id)
                try:
                    await sync_ticket(
                        engine=engine,
                        session_factory=async_session_factory,
                        ticket_id=ticket.id,
                        client=client,
                    )
                except Exception:
                    logger.exception("post sync failed for ticket %d", ticket.id)
                return
            else:  # "no"
                async with async_session_factory() as session:
                    existing = await get_draft(session, decoded.draft_id)
                    if existing is None or existing.user_id != user.id:
                        await answer("Черновик не найден", show_alert=True)
                        return
                    await delete_draft(session, draft_id=decoded.draft_id)
                    await session.commit()
                await answer("Создание отменено")
                return
        except DraftNotFound:
            await answer("Черновик не найден", show_alert=True)
            return
        except DraftChanged:
            await answer("Черновик устарел, начните заново", show_alert=True)
            return
        except DraftValidationError as exc:
            await answer(str(exc), show_alert=True)
            return
        except PermissionDenied:
            await answer("Недостаточно прав", show_alert=True)
            return

        await answer()
    finally:
        await answer()


async def draft_text_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a private-chat text message as a (possibly bound) form answer."""
    message = update.effective_message
    if message is None:
        return
    chat = update.effective_chat
    if chat is None or chat.type != Chat.PRIVATE:
        return
    if message.text is None:
        return

    user = cast(dict[str, Any], context.user_data).get("user")
    if user is None:
        return

    validation_error: str | None = None

    async with async_session_factory() as session:
        draft = await get_active_draft(session, user.id)
        if draft is None:
            return

        reply = message.reply_to_message
        bound = (
            reply is not None
            and draft.prompt_message_id is not None
            and reply.message_id == draft.prompt_message_id
        )

        if bound and draft.step is DraftStep.description:
            try:
                draft = await save_description(
                    session,
                    draft_id=draft.id,
                    expected_version=draft.version,
                    description=message.text,
                )
                await session.commit()
            except DraftValidationError as exc:
                validation_error = str(exc)
        elif bound and draft.step is DraftStep.request_id:
            try:
                draft = await save_request_id(
                    session,
                    draft_id=draft.id,
                    expected_version=draft.version,
                    request_id=message.text,
                )
                await session.commit()
            except DraftValidationError as exc:
                validation_error = str(exc)
        elif bound and draft.step is DraftStep.tenant_id:
            try:
                draft = await save_tenant_id(
                    session,
                    draft_id=draft.id,
                    expected_version=draft.version,
                    tenant_id=message.text,
                )
                await session.commit()
            except DraftValidationError as exc:
                validation_error = str(exc)
        # else: not bound, or a priority/confirm step — re-ask without mutating.

    if validation_error is not None:
        await message.reply_text(validation_error)

    await _send_step_prompt(context, chat.id, draft)
