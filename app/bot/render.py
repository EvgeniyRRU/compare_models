"""Russian UI strings: ticket post rendering and inline keyboards."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.db.models import Priority, Ticket, TicketStatus, User
from app.domain.state_machine import Action, available_buttons

MAX_POST_LEN = 4096

STATUS_LABELS: dict[TicketStatus, str] = {
    TicketStatus.new: "Новое",
    TicketStatus.in_progress: "В работе",
    TicketStatus.waiting_info: "Ожидает информации",
    TicketStatus.resolved: "Решено",
    TicketStatus.rejected: "Отклонено",
    TicketStatus.duplicate: "Дубль",
}

PRIORITY_LABELS: dict[Priority, str] = {
    Priority.critical: "Критический",
    Priority.high: "Высокий",
    Priority.medium: "Средний",
    Priority.low: "Низкий",
}

BUTTON_LABELS: dict[Action, str] = {
    Action.take: "Взять",
    Action.to_progress: "В работу",
    Action.to_waiting: "Ожидание",
    Action.resolve: "Решено",
    Action.reject: "Отклонено",
    Action.duplicate: "Дубль",
    Action.abandon: "Отказаться",
}


def format_user(user: User | None) -> str:
    if user is None:
        return "—"
    if user.username:
        return f"@{user.username}"
    return user.full_name or f"id{user.telegram_id}"


def callback_data(action: Action, ticket_id: int, version: int) -> str:
    return f"t:{action.value}:{ticket_id}:{version}"


def render_post(ticket: Ticket, author: User | None, assignee: User | None) -> str:
    """Post text for the dev chat, always ≤4096 chars (description shrinks first)."""
    header = "\n".join(
        (
            f"№ {ticket.id} · {STATUS_LABELS[ticket.status]}",
            f"Приоритет: {PRIORITY_LABELS[ticket.priority]}",
            f"Заявка: {ticket.request_id}",
            f"Арендатор: {ticket.tenant_id or '—'}",
            f"Автор: {format_user(author)}",
            f"Исполнитель: {format_user(assignee)}",
            "",
        )
    )
    budget = MAX_POST_LEN - len(header)
    description = ticket.description
    if budget < 1:
        return header[: MAX_POST_LEN - 1] + "…"
    if len(description) > budget:
        description = description[: budget - 1] + "…"
    return header + description


def build_markup(ticket: Ticket) -> InlineKeyboardMarkup | None:
    """Inline keyboard for the post; None for closed tickets (reopen = Mini-App)."""
    actions = available_buttons(ticket.status)
    if not actions:
        return None
    rows: list[list[InlineKeyboardButton]] = []
    if Action.take in actions:
        rows.append(
            [
                InlineKeyboardButton(
                    BUTTON_LABELS[Action.take],
                    callback_data=callback_data(Action.take, ticket.id, ticket.version),
                )
            ]
        )
        return InlineKeyboardMarkup(rows)
    if Action.to_progress in actions:
        move = Action.to_progress
    elif Action.to_waiting in actions:
        move = Action.to_waiting
    else:  # pragma: no cover - active statuses always have a move action
        move = None
    if move is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    BUTTON_LABELS[move],
                    callback_data=callback_data(move, ticket.id, ticket.version),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                BUTTON_LABELS[act],
                callback_data=callback_data(act, ticket.id, ticket.version),
            )
            for act in (Action.resolve, Action.reject, Action.duplicate)
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                BUTTON_LABELS[Action.abandon],
                callback_data=callback_data(Action.abandon, ticket.id, ticket.version),
            )
        ]
    )
    return InlineKeyboardMarkup(rows)
