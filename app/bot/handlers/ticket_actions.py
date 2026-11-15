"""Callback handler for ticket-action inline buttons (``t:<code>:<id>:<version>``)."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast

from telegram import Update
from telegram.ext import ContextTypes

from app.bot.callbacks import decode_ticket_callback
from app.bot.post_client import BotPostClient
from app.config import settings
from app.db.models import Ticket
from app.db.session import async_session_factory, engine
from app.logging import bind_contextvars
from app.services.post_sync import sync_ticket
from app.services.state_machine import TicketAction
from app.services.ticket_service import (
    PermissionDenied,
    TicketAlreadyTaken,
    TicketChanged,
    abandon,
    mark_duplicate,
    reject,
    resolve,
    set_in_progress,
    set_waiting_info,
    take,
)

logger = logging.getLogger(__name__)

#: Maps each coded ticket action to its service method. Every callback-code action is
#: present here (``reopen``/``reassign`` have no callback code).
_ACTION_METHODS: dict[TicketAction, Callable[..., Awaitable[Ticket]]] = {
    TicketAction.take: take,
    TicketAction.to_waiting: set_waiting_info,
    TicketAction.to_progress: set_in_progress,
    TicketAction.resolve: resolve,
    TicketAction.reject: reject,
    TicketAction.duplicate: mark_duplicate,
    TicketAction.abandon: abandon,
}


async def _resync(ticket_id: int, client: BotPostClient) -> None:
    """Re-sync a ticket's dev-chat post; failures are logged, never raised."""
    try:
        await sync_ticket(
            engine=engine,
            session_factory=async_session_factory,
            ticket_id=ticket_id,
            client=client,
        )
    except Exception:
        logger.exception("post re-sync failed for ticket %d", ticket_id)


async def ticket_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Decode a ticket button press, apply the transition, re-sync the post, and answer."""
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

    try:
        decoded = decode_ticket_callback(query.data or "")
        if decoded is None:
            await answer("Неверные данные кнопки", show_alert=True)
            return

        bind_contextvars(ticket_id=decoded.ticket_id)

        actor = cast(dict[str, Any], context.user_data).get("user")
        if actor is None:
            await answer("Пользователь не найден", show_alert=True)
            return

        method = _ACTION_METHODS[decoded.action]
        ticket_id = decoded.ticket_id
        client = BotPostClient(context.bot, settings.dev_chat_id)

        try:
            async with async_session_factory() as session:
                await method(
                    session,
                    ticket_id=ticket_id,
                    actor=actor,
                    expected_version=decoded.version,
                )
                await session.commit()
        except TicketChanged:
            await answer("Обращение изменилось", show_alert=True)
            await _resync(ticket_id, client)
            return
        except TicketAlreadyTaken:
            await answer("Уже взято", show_alert=True)
            await _resync(ticket_id, client)
            return
        except PermissionDenied:
            await answer("Недостаточно прав", show_alert=True)
            return

        await answer()  # close the spinner immediately; Telegram retries unanswered callbacks
        await _resync(ticket_id, client)
    finally:
        # Guarantee the callback is answered exactly once so the client's spinner
        # never hangs (e.g. on an unexpected exception the error handler logs).
        await answer()
