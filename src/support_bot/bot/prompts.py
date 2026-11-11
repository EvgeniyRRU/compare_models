from dataclasses import dataclass
from html import escape

from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup

from support_bot.services.drafts import DraftState, draft_callback
from support_bot.services.post_render import PRIORITY_TITLES
from support_bot.services.validation import DESCRIPTION_MAX, ID_MAX

CONFIRM_TITLES = {"ok": "Отправить", "no": "Отменить"}
SKIP_TITLE = "Пропустить"


@dataclass(frozen=True, slots=True)
class Question:
    text: str
    markup: InlineKeyboardMarkup | ForceReply
    # Text steps are bound to the question's message id through ForceReply.
    expects_reply: bool
    # Skip button for the tenant step is sent as a separate message, because Telegram
    # allows only one reply_markup per message.
    extra: InlineKeyboardMarkup | None = None


def question_for(state: DraftState) -> Question:
    if state.step == "description":
        return Question(
            text=f"Опишите проблему (до {DESCRIPTION_MAX} символов). Ответьте на это сообщение.",
            markup=ForceReply(selective=False),
            expects_reply=True,
        )
    if state.step == "priority":
        buttons = [
            InlineKeyboardButton(
                PRIORITY_TITLES[p],
                callback_data=draft_callback("pr", state.draft_id, state.version, p),
            )
            for p in ("critical", "high", "medium", "low")
        ]
        return Question(
            text="Выберите приоритет:",
            markup=InlineKeyboardMarkup([buttons[:2], buttons[2:]]),
            expects_reply=False,
        )
    if state.step == "request_id":
        return Question(
            text=f"Укажите ID заявки (до {ID_MAX} символов). Ответьте на это сообщение.",
            markup=ForceReply(selective=False),
            expects_reply=True,
        )
    if state.step == "tenant_id":
        skip = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        SKIP_TITLE,
                        callback_data=draft_callback("skip", state.draft_id, state.version),
                    )
                ]
            ]
        )
        return Question(
            text=(
                f"Укажите ID арендатора (до {ID_MAX} символов). "
                "Ответьте на это сообщение или пропустите шаг."
            ),
            markup=ForceReply(selective=False),
            expects_reply=True,
            extra=skip,
        )
    return Question(
        text=_summary(state),
        markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        CONFIRM_TITLES["ok"],
                        callback_data=draft_callback("ok", state.draft_id, state.version),
                    ),
                    InlineKeyboardButton(
                        CONFIRM_TITLES["no"],
                        callback_data=draft_callback("no", state.draft_id, state.version),
                    ),
                ]
            ]
        ),
        expects_reply=False,
    )


def _summary(state: DraftState) -> str:
    values = state.values
    priority = values.get("priority")
    lines = [
        "<b>Проверьте обращение</b>",
        f"Описание: {escape(values.get('description') or '—')}",
        f"Приоритет: {escape(PRIORITY_TITLES[priority]) if priority else '—'}",
        f"ID заявки: {escape(values.get('request_id') or '—')}",
        f"ID арендатора: {escape(values.get('tenant_id') or '—')}",
    ]
    return "\n".join(lines)
