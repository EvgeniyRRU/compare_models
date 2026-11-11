from dataclasses import dataclass
from datetime import datetime
from html import escape

from support_bot.domain.state_machine import Action, Status

TELEGRAM_LIMIT = 4096
ELLIPSIS = "…"
CALLBACK_PREFIX = "t"

STATUS_TITLES = {
    Status.NEW: "Новое",
    Status.IN_PROGRESS: "В работе",
    Status.WAITING_INFO: "Ожидание информации",
    Status.RESOLVED: "Решено",
    Status.REJECTED: "Отклонено",
    Status.DUPLICATE: "Дубль",
}
PRIORITY_TITLES = {
    "critical": "Критичный",
    "high": "Высокий",
    "medium": "Средний",
    "low": "Низкий",
}
BUTTON_TITLES = {
    Action.TAKE: "Взять",
    Action.WAIT_INFO: "В ожидании информации",
    Action.RESUME: "В работу",
    Action.RESOLVE: "Решено",
    Action.REJECT: "Отклонено",
    Action.DUPLICATE: "Дубль",
    Action.RELEASE: "Отказаться",
}
# Short codes keep `t:<code>:<id>:<version>` within Telegram's 64-byte callback limit.
ACTION_CODES = {
    "take": Action.TAKE,
    "wait": Action.WAIT_INFO,
    "work": Action.RESUME,
    "resolve": Action.RESOLVE,
    "reject": Action.REJECT,
    "dup": Action.DUPLICATE,
    "release": Action.RELEASE,
}
_CODE_BY_ACTION = {action: code for code, action in ACTION_CODES.items()}
_ACTIONS_BY_STATUS = {
    Status.NEW: [Action.TAKE],
    Status.IN_PROGRESS: [
        Action.WAIT_INFO,
        Action.RESOLVE,
        Action.REJECT,
        Action.DUPLICATE,
        Action.RELEASE,
    ],
    Status.WAITING_INFO: [
        Action.RESUME,
        Action.RESOLVE,
        Action.REJECT,
        Action.DUPLICATE,
        Action.RELEASE,
    ],
}


@dataclass(frozen=True, slots=True)
class PostView:
    ticket_id: int
    version: int
    status: Status
    priority: str
    author: str
    assignee: str | None
    request_id: str
    tenant_id: str | None
    description: str
    created_at: datetime


def actions_for(status: Status) -> list[Action]:
    return list(_ACTIONS_BY_STATUS.get(status, []))


def callback_data(action: Action, *, ticket_id: int, version: int) -> str:
    return f"{CALLBACK_PREFIX}:{_CODE_BY_ACTION[action]}:{ticket_id}:{version}"


def parse_callback(data: str) -> tuple[Action, int, int] | None:
    parts = data.split(":")
    if len(parts) != 4 or parts[0] != CALLBACK_PREFIX or parts[1] not in ACTION_CODES:
        return None
    try:
        return ACTION_CODES[parts[1]], int(parts[2]), int(parts[3])
    except ValueError:
        return None


def _body(view: PostView, description: str) -> str:
    lines = [
        f"<b>Обращение #{view.ticket_id}</b> · приоритет: {escape(PRIORITY_TITLES[view.priority])}",
        f"Статус: {escape(STATUS_TITLES[view.status])}",
        f"Автор: {escape(view.author)}",
        f"Исполнитель: {escape(view.assignee) if view.assignee else '—'}",
        f"ID заявки: {escape(view.request_id)}",
    ]
    if view.tenant_id:
        lines.append(f"ID арендатора: {escape(view.tenant_id)}")
    lines.append("")
    lines.append(escape(description))
    return "\n".join(lines)


def render_post(view: PostView) -> str:
    """Render the post text. The result always fits Telegram's limit."""
    text = _body(view, view.description)
    if len(text) <= TELEGRAM_LIMIT:
        return text
    note = "\n\nПолный текст — в карточке Mini App"
    kept = view.description
    while kept:
        kept = kept[:-1]
        candidate = _body(view, kept.rstrip() + ELLIPSIS) + note
        if len(candidate) <= TELEGRAM_LIMIT:
            return candidate
    return _body(view, ELLIPSIS) + note
