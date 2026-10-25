"""Вопросы пошаговой формы. Отправка вопроса и условное сохранение prompt_message_id.

Транзакция во время запроса к Telegram не открыта: сначала коммит шага, затем отправка
вопроса, затем отдельное условное UPDATE (draft_id, version, step).
"""

from html import escape

from telegram import Bot, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from support_bot.callbacks import DraftAction, DraftCallback
from support_bot.domain.enums import PRIORITY_LABELS, DraftStep, Priority
from support_bot.domain.validation import DESCRIPTION_MAX_LEN, REQUEST_ID_MAX_LEN, TENANT_ID_MAX_LEN
from support_bot.services.drafts import DraftService, DraftSnapshot

_REPLY_HINT = "Ответьте на это сообщение. /cancel — отменить создание."

QUESTIONS: dict[DraftStep, str] = {
    DraftStep.DESCRIPTION: (
        f"<b>Шаг 1 из 4.</b> Опишите проблему одним сообщением (до {DESCRIPTION_MAX_LEN} "
        f"символов).\n\n{_REPLY_HINT}"
    ),
    DraftStep.PRIORITY: "<b>Шаг 2 из 4.</b> Выберите приоритет:",
    DraftStep.REQUEST_ID: (
        f"<b>Шаг 3 из 4.</b> Введите id заявки (до {REQUEST_ID_MAX_LEN} символов).\n\n{_REPLY_HINT}"
    ),
    DraftStep.TENANT_ID: (
        f"<b>Шаг 4 из 4.</b> Введите id арендатора (до {TENANT_ID_MAX_LEN} символов) "
        f"или нажмите «Пропустить» в следующем сообщении.\n\n{_REPLY_HINT}"
    ),
}

PLACEHOLDERS: dict[DraftStep, str] = {
    DraftStep.DESCRIPTION: "Описание проблемы",
    DraftStep.REQUEST_ID: "Id заявки",
    DraftStep.TENANT_ID: "Id арендатора",
}


def _cb(draft: DraftSnapshot, action: DraftAction, priority: Priority | None = None) -> str:
    return DraftCallback(action, draft.id, draft.version, priority).encode()


def priority_keyboard(draft: DraftSnapshot) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(PRIORITY_LABELS[p], callback_data=_cb(draft, DraftAction.PRIORITY, p))
        for p in Priority
    ]
    return InlineKeyboardMarkup([buttons[:2], buttons[2:]])


def _e(value: str | None) -> str:
    return escape(value or "—", quote=False)


def confirm_text(draft: DraftSnapshot) -> str:
    priority = PRIORITY_LABELS[draft.priority] if draft.priority else "—"
    return (
        "<b>Проверьте обращение:</b>\n\n"
        f"Приоритет: {priority}\n"
        f"Заявка: <code>{_e(draft.request_id)}</code>\n"
        f"Арендатор: {_e(draft.tenant_id)}\n\n"
        f"{_e(draft.description)}"
    )


def confirm_keyboard(draft: DraftSnapshot) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📨 Отправить", callback_data=_cb(draft, DraftAction.SUBMIT)),
                InlineKeyboardButton("✖️ Отменить", callback_data=_cb(draft, DraftAction.CANCEL)),
            ]
        ]
    )


def resume_keyboard(draft: DraftSnapshot) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "▶️ Продолжить", callback_data=_cb(draft, DraftAction.CONTINUE)
                ),
                InlineKeyboardButton(
                    "🔄 Начать заново", callback_data=_cb(draft, DraftAction.RESTART)
                ),
            ]
        ]
    )


async def ask(
    bot: Bot,
    chat_id: int,
    drafts: DraftService,
    draft: DraftSnapshot,
    notice: str | None = None,
) -> None:
    """Задать вопрос текущего шага и сохранить его message_id условным UPDATE."""
    prefix = f"{escape(notice, quote=False)}\n\n" if notice else ""
    markup: InlineKeyboardMarkup | ForceReply
    match draft.step:
        case DraftStep.PRIORITY:
            text, markup = prefix + QUESTIONS[draft.step], priority_keyboard(draft)
        case DraftStep.CONFIRM:
            text, markup = prefix + confirm_text(draft), confirm_keyboard(draft)
        case _:
            # ForceReply лишь помогает в интерфейсе; привязку ответа проверяет сервер.
            text = prefix + QUESTIONS[draft.step]
            markup = ForceReply(input_field_placeholder=PLACEHOLDERS[draft.step])

    message = await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=markup)
    await drafts.save_prompt(draft.id, draft.version, draft.step, message.message_id)

    if draft.step is DraftStep.TENANT_ID:
        await bot.send_message(
            chat_id,
            "Нет id арендатора?",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⏭ Пропустить", callback_data=_cb(draft, DraftAction.SKIP_TENANT)
                        )
                    ]
                ]
            ),
        )
