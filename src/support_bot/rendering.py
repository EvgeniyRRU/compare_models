"""Рендер поста обращения (HTML parse mode) с гарантией лимита 4096 символов."""

from dataclasses import dataclass
from datetime import datetime
from html import escape

from support_bot.callbacks import TicketCallback
from support_bot.domain.enums import PRIORITY_LABELS, STATUS_LABELS
from support_bot.domain.state_machine import POST_ACTIONS, Action
from support_bot.services.ports import Button, Keyboard
from support_bot.services.views import TicketView

TELEGRAM_TEXT_LIMIT = 4096
ELLIPSIS = "…"

ACTION_LABELS: dict[Action, str] = {
    Action.TAKE: "✋ Взять",
    Action.TO_WAITING: "⏳ В ожидании информации",
    Action.TO_WORK: "🛠 В работу",
    Action.RESOLVE: "✅ Решено",
    Action.REJECT: "🚫 Отклонено",
    Action.DUPLICATE: "♻️ Дубль",
    Action.RELEASE: "↩️ Отказаться",
}


@dataclass(frozen=True)
class RenderedPost:
    text: str
    keyboard: Keyboard
    version: int


def format_dt(value: datetime | None) -> str:
    return value.strftime("%d.%m.%Y %H:%M UTC") if value else "—"


def truncate_escaped(text: str, budget: int) -> str:
    """Обрезать исходный текст так, чтобы его HTML-escape (с «…») влез в budget."""
    escaped = escape(text, quote=False)
    if len(escaped) <= budget:
        return escaped
    if budget <= len(ELLIPSIS):
        return ELLIPSIS[: max(budget, 0)]
    lo, hi = 0, len(text)
    while lo < hi:  # максимальная длина префикса, который помещается
        mid = (lo + hi + 1) // 2
        if len(escape(text[:mid], quote=False)) + len(ELLIPSIS) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return escape(text[:lo].rstrip(), quote=False) + ELLIPSIS


def build_keyboard(view: TicketView) -> Keyboard:
    buttons = [
        Button(ACTION_LABELS[a], TicketCallback(a, view.id, view.version).encode())
        for a in POST_ACTIONS[view.status]
    ]
    if not buttons:
        return ()
    if len(buttons) == 1:
        return ((buttons[0],),)
    # Первая кнопка — на всю ширину, остальные по две в ряд.
    rows: list[tuple[Button, ...]] = [(buttons[0],)]
    rest = buttons[1:]
    rows.extend(tuple(rest[i : i + 2]) for i in range(0, len(rest), 2))
    return tuple(rows)


def e(value: str) -> str:
    return escape(value, quote=False)


def render_post(view: TicketView, limit: int = TELEGRAM_TEXT_LIMIT) -> RenderedPost:
    assignee = e(view.assignee.display_name) if view.assignee else "—"
    lines = [
        f"<b>Обращение #{view.id}</b>",
        f"Статус: {STATUS_LABELS[view.status]}",
        f"Приоритет: {PRIORITY_LABELS[view.priority]}",
        f"Заявка: <code>{e(view.request_id)}</code>",
        f"Арендатор: {f'<code>{e(view.tenant_id)}</code>' if view.tenant_id else '—'}",
        f"Автор: {e(view.author.display_name)}",
        f"Исполнитель: {assignee}",
        f"Создано: {format_dt(view.created_at)}",
    ]
    if view.closed_at:
        lines.append(f"Закрыто: {format_dt(view.closed_at)}")
    header = "\n".join(lines) + "\n\n"
    description = truncate_escaped(view.description, limit - len(header))
    text = header + description
    if len(text) > limit:  # pragma: no cover - страховка, шапка ограничена длинами полей
        text = text[:limit]
    return RenderedPost(text=text, keyboard=build_keyboard(view), version=view.version)


def render_reminder(view_id: int, priority_label: str) -> str:
    return f"⏰ Обращение #{view_id} ({priority_label}) всё ещё не взято. Нажмите «Взять» в посте."
