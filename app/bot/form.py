"""Форма создания обращения: шаги, вопросы, callback_data (<= 64 байт)."""

from __future__ import annotations

import uuid as _uuid
from typing import Any

from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup

STEPS = ("description", "priority", "request_id", "tenant_id", "confirm")
TEXT_STEPS = ("description", "request_id", "tenant_id")
PRIORITY_CHOICES = (
    ("critical", "Критический"),
    ("high", "Высокий"),
    ("medium", "Средний"),
    ("low", "Низкий"),
)
PRIORITY_SHORT = {"critical": "c", "high": "h", "medium": "m", "low": "l"}
SHORT_PRIORITY = {v: k for k, v in PRIORITY_SHORT.items()}
PRIORITY_LABEL = dict(PRIORITY_CHOICES)

QUESTIONS = {
    "description": "Опишите обращение (до 3000 символов):",
    "request_id": "Введите id заявки (до 64 символов):",
    "tenant_id": "Введите id арендатора (до 64 символов) или нажмите «Пропустить»:",
}


def _uid(draft: Any) -> str:
    return str(draft.id).replace("-", "")  # 32 hex


def cb(kind: str, draft: Any, *extra: str) -> str:
    """callback_data формы: d:<kind>:<uuid32>:<version>[:extra]. <= 64 байт."""
    data = ":".join(["d", kind, _uid(draft), str(draft.version), *extra])
    assert len(data.encode()) <= 64, f"callback_data too long: {len(data)}"
    return data


def question_for(step: str, draft: Any) -> tuple[str, Any]:
    """(text, reply_markup) вопроса текущего шага."""
    if step in QUESTIONS:
        return QUESTIONS[step], ForceReply(True)
    if step == "priority":
        keyboard = [
            [
                InlineKeyboardButton(label, callback_data=cb("prio", draft, PRIORITY_SHORT[p]))
                for p, label in PRIORITY_CHOICES
            ]
        ]
        return "Выберите приоритет:", InlineKeyboardMarkup(keyboard)
    # confirm
    lines = [
        "Проверьте данные:",
        f"• Описание: {draft.description}",
        f"• Приоритет: {PRIORITY_LABEL.get(draft.priority or '', draft.priority)}",
        f"• Id заявки: {draft.request_id}",
        f"• Id арендатора: {draft.tenant_id or '—'}",
    ]
    keyboard = [
        [
            InlineKeyboardButton("✅ Отправить", callback_data=cb("send", draft)),
            InlineKeyboardButton("✖ Отменить", callback_data=cb("canx", draft)),
        ]
    ]
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


def parse_draft_callback(data: str) -> tuple[str, str, int, str | None] | None:
    """d:<kind>:<uuid32>:<version>[:extra] → (kind, uuid_str, version, extra)."""
    parts = data.split(":")
    if len(parts) < 4 or parts[0] != "d":
        return None
    kind, uid, version = parts[1], parts[2], parts[3]
    extra = parts[4] if len(parts) > 4 else None
    try:
        _uuid.UUID(uid)
        version_int = int(version)
    except ValueError:
        return None
    return kind, uid, version_int, extra
