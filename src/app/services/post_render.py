import html
from dataclasses import dataclass

from app.db.models import Ticket, User
from app.domain.state_machine import Action, Status

TELEGRAM_LIMIT = 4096
_PRIORITY = {"critical": "🔴 critical", "high": "🟠 high", "medium": "🟡 medium", "low": "🟢 low"}
_STATUS = {
    Status.NEW: "🆕 Новое",
    Status.IN_PROGRESS: "🛠 В работе",
    Status.WAITING_INFO: "⏳ Ожидает информации",
    Status.RESOLVED: "✅ Решено",
    Status.REJECTED: "🚫 Отклонено",
    Status.DUPLICATE: "♻️ Дубль",
}
STATUS_LABELS = {k.value: v for k, v in _STATUS.items()}


@dataclass(frozen=True)
class Button:
    label: str
    callback_data: str


@dataclass(frozen=True)
class PostView:
    text: str
    buttons: list[list[Button]]


# код действия в callback_data: t:<код>:<id>:<version>
ACTION_CODES = {
    Action.TAKE: "tk",
    Action.WAIT: "wt",
    Action.RESUME: "ip",
    Action.RESOLVE: "rs",
    Action.REJECT: "rj",
    Action.DUPLICATE: "dp",
    Action.RELEASE: "rl",
}
CODE_ACTIONS = {v: k for k, v in ACTION_CODES.items()}


def _cb(action: Action, ticket: Ticket) -> str:
    return f"t:{ACTION_CODES[action]}:{ticket.id}:{ticket.version}"


def _buttons(ticket: Ticket) -> list[list[Button]]:
    status = Status(ticket.status)
    if status == Status.NEW:
        return [[Button("Взять", _cb(Action.TAKE, ticket))]]
    if status in (Status.IN_PROGRESS, Status.WAITING_INFO):
        toggle = (
            Button("В ожидании информации", _cb(Action.WAIT, ticket))
            if status == Status.IN_PROGRESS
            else Button("В работу", _cb(Action.RESUME, ticket))
        )
        return [
            [toggle],
            [
                Button("Решено", _cb(Action.RESOLVE, ticket)),
                Button("Отклонено", _cb(Action.REJECT, ticket)),
                Button("Дубль", _cb(Action.DUPLICATE, ticket)),
            ],
            [Button("Отказаться", _cb(Action.RELEASE, ticket))],
        ]
    return []


def render_post(ticket: Ticket, author: User, assignee: User | None) -> PostView:
    e = html.escape

    def build(description: str) -> str:
        lines = [
            f"<b>#{ticket.id}</b> · {_PRIORITY[ticket.priority]}",
            f"Статус: {_STATUS[Status(ticket.status)]}",
            f"Заявка: <code>{e(ticket.request_id)}</code>",
        ]
        if ticket.tenant_id:
            lines.append(f"Арендатор: <code>{e(ticket.tenant_id)}</code>")
        lines.append(f"Автор: {e(author.display_name)}")
        if assignee:
            lines.append(f"Исполнитель: {e(assignee.display_name)}")
        lines.append("")
        lines.append(e(description))
        return "\n".join(lines)

    description = ticket.description
    text = build(description)
    if len(text) > TELEGRAM_LIMIT:
        # HTML-экранирование раздувает текст: подбираем длину обрезки по итоговому размеру
        lo, hi = 0, len(description)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if len(build(description[:mid].rstrip() + "…")) <= TELEGRAM_LIMIT:
                lo = mid
            else:
                hi = mid - 1
        text = build(description[:lo].rstrip() + "…")
    return PostView(text=text, buttons=_buttons(ticket))
