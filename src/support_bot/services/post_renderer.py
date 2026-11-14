from __future__ import annotations

import html
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from support_bot.db.enums import TicketStatus
from support_bot.db.models.ticket import Ticket
from support_bot.domain.labels import PRIORITY_LABELS, STATUS_LABELS

TEXT_LIMIT = 4096
ELLIPSIS = "…"


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _cut_utf16(text: str, limit: int) -> str:
    used = 0
    out: list[str] = []
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if used + width > limit:
            break
        out.append(char)
        used += width
    return "".join(out)


def truncate_utf16(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if utf16_len(text) <= limit:
        return text
    return _cut_utf16(text, limit - utf16_len(ELLIPSIS)) + ELLIPSIS


def truncate_html_utf16(text: str, limit: int) -> str:
    """Truncate HTML-escaped text without leaving a broken entity behind."""
    if limit <= 0:
        return ""
    if utf16_len(text) <= limit:
        return text
    truncated = _cut_utf16(text, limit - utf16_len(ELLIPSIS))
    ampersand = truncated.rfind("&")
    if ampersand != -1 and ";" not in truncated[ampersand:]:
        truncated = truncated[:ampersand]
    return truncated + ELLIPSIS


def _format_dt(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.strftime("%d.%m.%Y %H:%M UTC")


def _format_user(name: str, username: str | None) -> str:
    escaped = html.escape(name)
    if username:
        return f"{escaped} (@{html.escape(username)})"
    return escaped


def action_keyboard(ticket: Ticket) -> InlineKeyboardMarkup | None:
    prefix = f"t:{{}}:{ticket.id}:{ticket.version}"
    rows: list[list[InlineKeyboardButton]] = []
    if ticket.status is TicketStatus.NEW:
        rows.append([InlineKeyboardButton("Взять", callback_data=prefix.format("k"))])
    elif ticket.status is TicketStatus.IN_PROGRESS:
        rows.append(
            [
                InlineKeyboardButton("В ожидании информации", callback_data=prefix.format("w")),
                InlineKeyboardButton("Решено", callback_data=prefix.format("r")),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton("Отклонено", callback_data=prefix.format("j")),
                InlineKeyboardButton("Дубль", callback_data=prefix.format("d")),
                InlineKeyboardButton("Отказаться", callback_data=prefix.format("x")),
            ]
        )
    elif ticket.status is TicketStatus.WAITING_INFO:
        rows.append(
            [
                InlineKeyboardButton("В работу", callback_data=prefix.format("p")),
                InlineKeyboardButton("Решено", callback_data=prefix.format("r")),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton("Отклонено", callback_data=prefix.format("j")),
                InlineKeyboardButton("Дубль", callback_data=prefix.format("d")),
                InlineKeyboardButton("Отказаться", callback_data=prefix.format("x")),
            ]
        )
    if not rows:
        return None
    return InlineKeyboardMarkup(rows)


def render_ticket_post(ticket: Ticket) -> tuple[str, InlineKeyboardMarkup | None]:
    """Render a post guaranteed to fit Telegram's 4096 UTF-16 code units."""
    author = ticket.author
    assignee = ticket.assignee

    header = [
        f"<b>#{ticket.id}</b> · {PRIORITY_LABELS[ticket.priority]}",
        f"Статус: <b>{STATUS_LABELS[ticket.status]}</b>",
        f"Автор: {_format_user(author.display_name, author.username)}",
        "Исполнитель: "
        + (_format_user(assignee.display_name, assignee.username) if assignee is not None else "—"),
        f"Заявка: {html.escape(ticket.request_id)}",
        f"Арендатор: {html.escape(ticket.tenant_id) if ticket.tenant_id else '—'}",
        f"Создано: {_format_dt(ticket.created_at)}",
        f"Взято: {_format_dt(ticket.taken_at)}",
    ]
    if ticket.closed_at is not None:
        header.append(f"Закрыто: {_format_dt(ticket.closed_at)}")

    prefix = "\n".join(header) + "\n\nОписание:\n"
    budget = TEXT_LIMIT - utf16_len(prefix)
    body = prefix + truncate_html_utf16(html.escape(ticket.description), budget)
    if utf16_len(body) > TEXT_LIMIT:
        body = truncate_html_utf16(body, TEXT_LIMIT)
    return body, action_keyboard(ticket)
