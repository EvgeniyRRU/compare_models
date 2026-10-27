"""Russian plain-text Telegram views with UTF-16-safe message limits."""

from collections.abc import Sequence

from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup

from support_bot.db.models import Ticket, TicketDraft, User

STATUSES = {
    "new": "Новый",
    "in_progress": "В работе",
    "waiting_info": "В ожидании информации",
    "resolved": "Решено",
    "rejected": "Отклонено",
    "duplicate": "Дубль",
}
PRIORITIES = {"critical": "Критический", "high": "Высокий", "medium": "Средний", "low": "Низкий"}
ACTIONS = {
    "take": "Взять",
    "wait": "В ожидании информации",
    "work": "В работу",
    "resolve": "Решено",
    "reject": "Отклонено",
    "duplicate": "Дубль",
    "refuse": "Отказаться",
    "continue": "Продолжить",
    "restart": "Начать заново",
    "skip": "Пропустить",
    "submit": "Отправить",
    "cancel": "Отменить",
    **{"p" + key: value for key, value in PRIORITIES.items()},
}


def _units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _truncate(value: str, units: int) -> str:
    if _units(value) <= units:
        return value
    return (
        value.encode("utf-16-le")[: max(0, units - 1) * 2].decode("utf-16-le", errors="ignore")
        + "…"
    )


def _person(user: User | None) -> str:
    if user is None:
        return "Не назначен"
    name = " ".join(part for part in (user.first_name, user.last_name) if part)
    suffix = f" (@{user.username})" if user.username else f" (ID {user.telegram_id})"
    return _truncate(name + suffix, 600)


def render_post(ticket: Ticket, author: User, assignee: User | None) -> str:
    prefix = (
        f"Обращение #{ticket.id}\n"
        f"Приоритет: {PRIORITIES[ticket.priority]}\n"
        f"Заявка: {ticket.request_id}\n"
        f"Арендатор: {ticket.tenant_id or '—'}\n"
        f"Автор: {_person(author)}\n"
        f"Статус: {STATUSES[ticket.status]}\n"
        f"Исполнитель: {_person(assignee)}\n\n"
    )
    return prefix + _truncate(ticket.description, 4096 - _units(prefix))


def ticket_keyboard(ticket: Ticket) -> InlineKeyboardMarkup:
    actions = []
    if ticket.status == "new":
        actions = ["take"]
    elif ticket.status in {"in_progress", "waiting_info"}:
        actions = [
            "wait" if ticket.status == "in_progress" else "work",
            "resolve",
            "reject",
            "duplicate",
            "refuse",
        ]
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    ACTIONS[action], callback_data=f"t:{action}:{ticket.id}:{ticket.version}"
                )
            ]
            for action in actions
        ]
    )


def draft_keyboard(draft: TicketDraft, actions: Sequence[str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    ACTIONS[action], callback_data=f"d:{draft.id.hex}:{draft.version}:{action}"
                )
            ]
            for action in actions
        ]
    )


def draft_prompt(draft: TicketDraft) -> tuple[str, InlineKeyboardMarkup | ForceReply]:
    prompts = {
        "description": "Опишите обращение (до 3000 символов). Для отмены: /cancel.",
        "request_id": "Введите ID заявки (до 64 символов). Для отмены: /cancel.",
        "tenant_id": "Введите ID арендатора (до 64 символов) или нажмите «Пропустить».",
    }
    if draft.step in prompts:
        return prompts[draft.step], ForceReply(selective=True)
    if draft.step == "priority":
        return "Выберите приоритет:", draft_keyboard(draft, ["p" + key for key in PRIORITIES])
    text = (
        f"Подтвердите обращение\nПриоритет: {PRIORITIES[draft.priority or 'medium']}\n"
        f"Заявка: {draft.request_id}\nАрендатор: {draft.tenant_id or '—'}\n\n"
    )
    return text + _truncate(draft.description or "", 4096 - _units(text)), draft_keyboard(
        draft, ["submit", "cancel"]
    )
