"""Inline keyboard builders for dev-chat ticket posts and draft forms."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.callbacks import encode_form_callback, encode_ticket_callback
from app.db.enums import Priority, TicketStatus
from app.db.models import Ticket, TicketDraft
from app.services.render import PRIORITY_LABELS
from app.services.state_machine import TicketAction

TICKET_ACTION_LABELS: dict[TicketAction, str] = {
    TicketAction.take: "Взять",
    TicketAction.to_waiting: "В ожидании информации",
    TicketAction.to_progress: "В работу",
    TicketAction.resolve: "Решено",
    TicketAction.reject: "Отклонено",
    TicketAction.duplicate: "Дубль",
    TicketAction.abandon: "Отказаться",
}

#: Which actions to offer for each actionable status. Every button is rendered
#: for every viewer; the server enforces permissions when the press arrives.
_STATUS_ACTIONS: dict[TicketStatus, tuple[TicketAction, ...]] = {
    TicketStatus.new: (TicketAction.take,),
    TicketStatus.in_progress: (
        TicketAction.to_waiting,
        TicketAction.resolve,
        TicketAction.reject,
        TicketAction.duplicate,
        TicketAction.abandon,
    ),
    TicketStatus.waiting_info: (
        TicketAction.to_progress,
        TicketAction.resolve,
        TicketAction.reject,
        TicketAction.duplicate,
        TicketAction.abandon,
    ),
}


def ticket_action_keyboard(ticket: Ticket) -> InlineKeyboardMarkup | None:
    """Return the action keyboard for a ticket, or ``None`` for final statuses."""
    actions = _STATUS_ACTIONS.get(ticket.status)
    if actions is None:
        return None
    buttons = [
        InlineKeyboardButton(
            text=TICKET_ACTION_LABELS[action],
            callback_data=encode_ticket_callback(action, ticket.id, ticket.version),
        )
        for action in actions
    ]
    return InlineKeyboardMarkup(inline_keyboard=[[button] for button in buttons])


def priority_keyboard(draft: TicketDraft) -> InlineKeyboardMarkup:
    """Return the four priority buttons for the priority step."""
    buttons = [
        InlineKeyboardButton(
            text=PRIORITY_LABELS[priority],
            callback_data=encode_form_callback("priority", draft.id, draft.version, priority),
        )
        for priority in Priority
    ]
    return InlineKeyboardMarkup(inline_keyboard=[[button] for button in buttons])


def confirm_keyboard(draft: TicketDraft) -> InlineKeyboardMarkup:
    """Return the send/cancel buttons for the confirm step."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Отправить",
                    callback_data=encode_form_callback("ok", draft.id, draft.version),
                ),
                InlineKeyboardButton(
                    text="Отменить",
                    callback_data=encode_form_callback("no", draft.id, draft.version),
                ),
            ]
        ]
    )


def tenant_id_keyboard(draft: TicketDraft) -> InlineKeyboardMarkup:
    """Return the skip button for the tenant-id step."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Пропустить",
                    callback_data=encode_form_callback("skip", draft.id, draft.version),
                )
            ]
        ]
    )
