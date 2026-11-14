"""Pure post-text rendering for the dev-chat ticket posts (no I/O)."""

from app.db.enums import Priority, TicketStatus
from app.db.models import Ticket, User

STATUS_LABELS: dict[TicketStatus, str] = {
    TicketStatus.new: "Новое",
    TicketStatus.in_progress: "В работе",
    TicketStatus.waiting_info: "Ожидает информации",
    TicketStatus.resolved: "Решено",
    TicketStatus.rejected: "Отклонено",
    TicketStatus.duplicate: "Дубль",
}

PRIORITY_LABELS: dict[Priority, str] = {
    Priority.critical: "Критичный",
    Priority.high: "Высокий",
    Priority.medium: "Средний",
    Priority.low: "Низкий",
}

#: Telegram's hard limit on a single message's text length.
_MAX_POST_LENGTH = 4096
_TRUNCATION_MARK = "…"


def user_display_name(user: User | None) -> str:
    """Render a user as ``full_name``, else ``username``, else ``telegram_id``."""
    if user is None:
        return "—"
    if user.full_name:
        return user.full_name
    if user.username:
        return user.username
    return str(user.telegram_id)


def _truncate(text: str, limit: int) -> str:
    """Return ``text`` truncated to at most ``limit`` characters.

    When truncation happens the trailing ``…`` marks the cut, keeping the result
    exactly ``limit`` characters (or as many as fit when ``limit`` is tiny).
    """
    if len(text) <= limit:
        return text
    if limit <= len(_TRUNCATION_MARK):
        return _TRUNCATION_MARK[:limit]
    return text[: limit - len(_TRUNCATION_MARK)] + _TRUNCATION_MARK


def render_post_text(ticket: Ticket, *, author: User | None, assignee: User | None) -> str:
    """Render a ticket's dev-chat post, guaranteed to be at most 4096 characters.

    The header (number, priority, request id, optional tenant, author, status,
    optional assignee) is rendered first; the description is then truncated to
    fit the remaining budget so the final text never exceeds the Telegram limit.
    """
    lines = [
        f"#{ticket.id} · {PRIORITY_LABELS[ticket.priority]}",
        f"Статус: {STATUS_LABELS[ticket.status]}",
        f"Заявка: {ticket.request_id}",
    ]
    if ticket.tenant_id:
        lines.append(f"Арендатор: {ticket.tenant_id}")
    lines.append(f"Автор: {user_display_name(author)}")
    if assignee is not None:
        lines.append(f"Исполнитель: {user_display_name(assignee)}")

    header = "\n".join(lines)
    separator = "\n\n"
    # Defensive clamp: keep the invariant local rather than relying on the DB's
    # field-length constraints to keep the header under the limit.
    budget = max(0, _MAX_POST_LENGTH - len(header) - len(separator))
    return header + separator + _truncate(ticket.description, budget)
