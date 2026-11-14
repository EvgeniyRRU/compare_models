from __future__ import annotations

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from support_bot.bot.callbacks import parse_ticket_callback
from support_bot.bot.common import get_services, upsert_user
from support_bot.domain.errors import (
    AlreadyTaken,
    PermissionDenied,
    StatusConflict,
    TicketNotFound,
    VersionConflict,
)
from support_bot.domain.state_machine import TicketAction
from support_bot.observability.metrics import observe_error
from support_bot.observability.sentry import capture_exception
from support_bot.services import tickets as tickets_service

logger = structlog.get_logger(__name__)

ACTION_BY_CODE: dict[str, TicketAction] = {
    "k": TicketAction.TAKE,
    "w": TicketAction.WAIT_INFO,
    "p": TicketAction.TO_WORK,
    "r": TicketAction.RESOLVE,
    "j": TicketAction.REJECT,
    "d": TicketAction.DUPLICATE,
    "x": TicketAction.DECLINE,
}


async def on_ticket_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    services = get_services(context)
    if query.message is None or query.message.chat.id != services.settings.dev_chat_id:
        await query.answer("Действие доступно только в чате разработчиков.", show_alert=True)
        return

    parsed = parse_ticket_callback(query.data)
    if parsed is None:
        await query.answer("Кнопка устарела.", show_alert=True)
        return
    code, ticket_id, version = parsed
    action = ACTION_BY_CODE.get(code)
    if action is None:
        await query.answer("Кнопка устарела.", show_alert=True)
        return

    user = await upsert_user(update, services)
    if user is None:
        return

    answer_text = "Готово"
    show_alert = False
    should_sync = True
    try:
        async with services.session_factory() as session:
            if action is TicketAction.TAKE:
                await tickets_service.take_ticket(
                    session,
                    ticket_id=ticket_id,
                    expected_version=version,
                    actor_id=user.id,
                )
            else:
                await tickets_service.change_status(
                    session,
                    ticket_id=ticket_id,
                    expected_version=version,
                    action=action,
                    actor_id=user.id,
                    policy=services.policy,
                )
    except AlreadyTaken:
        answer_text = "Уже взято"
        show_alert = True
    except VersionConflict, StatusConflict:
        answer_text = "Обращение изменилось"
        show_alert = True
    except PermissionDenied:
        answer_text = "Недостаточно прав"
        show_alert = True
    except TicketNotFound:
        answer_text = "Обращение не найдено"
        show_alert = True
        should_sync = False
    except Exception as exc:
        logger.exception("ticket callback failed", component="bot", ticket_id=ticket_id)
        observe_error("bot")
        capture_exception(exc, component="bot", ticket_id=ticket_id)
        answer_text = "Ошибка обработки действия"
        show_alert = True

    await query.answer(answer_text, show_alert=show_alert)
    if should_sync:
        try:
            await services.post_sync.sync(ticket_id)
        except Exception as exc:
            logger.exception(
                "post sync after ticket action failed",
                component="bot",
                ticket_id=ticket_id,
            )
            observe_error("bot")
            capture_exception(exc, component="bot", ticket_id=ticket_id)
