from __future__ import annotations

import html

from telegram import (
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from support_bot.bot.callbacks import draft_callback
from support_bot.db.enums import DraftStep, TicketPriority
from support_bot.domain.labels import PRIORITY_LABELS
from support_bot.services.drafts import DraftSnapshot

PRIORITY_ACTIONS: dict[str, TicketPriority] = {
    "pc": TicketPriority.CRITICAL,
    "ph": TicketPriority.HIGH,
    "pm": TicketPriority.MEDIUM,
    "pl": TicketPriority.LOW,
}

PRIORITY_CODE: dict[TicketPriority, str] = {
    priority: code for code, priority in PRIORITY_ACTIONS.items()
}


def mini_app_keyboard(url: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("Открыть приложение", web_app=WebAppInfo(url=url))]],
        resize_keyboard=True,
        is_persistent=False,
    )


def continue_or_restart(snapshot: DraftSnapshot) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Продолжить", callback_data=draft_callback(snapshot.id, snapshot.version, "c")
                ),
                InlineKeyboardButton(
                    "Начать заново",
                    callback_data=draft_callback(snapshot.id, snapshot.version, "r"),
                ),
            ]
        ]
    )


def render_step(
    snapshot: DraftSnapshot,
) -> tuple[str, InlineKeyboardMarkup | ForceReply | None]:
    if snapshot.step is DraftStep.DESCRIPTION:
        return (
            "Шаг 1/5. Введите описание обращения (до 3000 символов):",
            ForceReply(selective=True),
        )
    if snapshot.step is DraftStep.PRIORITY:
        rows = [
            [
                InlineKeyboardButton(
                    PRIORITY_LABELS[priority],
                    callback_data=draft_callback(snapshot.id, snapshot.version, code),
                )
                for code, priority in list(PRIORITY_ACTIONS.items())[index : index + 2]
            ]
            for index in (0, 2)
        ]
        return ("Шаг 2/5. Выберите приоритет:", InlineKeyboardMarkup(rows))
    if snapshot.step is DraftStep.REQUEST_ID:
        return (
            "Шаг 3/5. Введите ID заявки (до 64 символов):",
            ForceReply(selective=True),
        )
    if snapshot.step is DraftStep.TENANT_ID:
        return (
            "Шаг 4/5. Введите ID арендатора (до 64 символов) или пропустите шаг:",
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Пропустить",
                            callback_data=draft_callback(snapshot.id, snapshot.version, "s"),
                        )
                    ]
                ]
            ),
        )

    description = snapshot.description or ""
    if len(description) > 800:
        description = description[:800] + "…"
    lines = [
        "Шаг 5/5. Проверьте обращение:",
        "",
        f"<b>Описание:</b>\n{html.escape(description)}",
        f"<b>Приоритет:</b> {PRIORITY_LABELS[snapshot.priority] if snapshot.priority else '—'}",
        f"<b>ID заявки:</b> {html.escape(snapshot.request_id or '—')}",
        f"<b>ID арендатора:</b> {html.escape(snapshot.tenant_id or '—')}",
    ]
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Отправить", callback_data=draft_callback(snapshot.id, snapshot.version, "o")
                ),
                InlineKeyboardButton(
                    "Отменить", callback_data=draft_callback(snapshot.id, snapshot.version, "x")
                ),
            ]
        ]
    )
    return ("\n".join(lines), keyboard)
