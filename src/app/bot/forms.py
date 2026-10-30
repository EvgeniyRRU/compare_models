"""Показ вопросов формы создания обращения (транзакция во время запросов к Telegram не держится)."""

import html

from telegram import Bot, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.context import AppContext
from app.domain.state_machine import Priority
from app.services.drafts import TEXT_STEPS, DraftView, Step, encode_id

PRIORITY_LABELS = {
    "critical": "🔴 Critical",
    "high": "🟠 High",
    "medium": "🟡 Medium",
    "low": "🟢 Low",
}

QUESTIONS = {
    Step.DESCRIPTION: "Опишите проблему (до 3000 символов). Ответьте на это сообщение (reply).",
    Step.PRIORITY: "Выберите приоритет:",
    Step.REQUEST_ID: "Укажите id заявки (до 64 символов). Ответьте на это сообщение (reply).",
    Step.TENANT_ID: "Укажите id арендатора (до 64 символов) ответом на это сообщение или пропустите.",
}


def cb(action: str, d: DraftView, value: str | None = None) -> str:
    data = f"d:{action}:{encode_id(d.id)}:{d.version}"
    return f"{data}:{value}" if value else data


def _btn(label: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=data)


def summary(d: DraftView) -> str:
    e = html.escape
    lines = [
        "<b>Проверьте обращение</b>",
        f"Приоритет: {PRIORITY_LABELS[d.data['priority']]}",
        f"Заявка: <code>{e(d.data['request_id'])}</code>",
        f"Арендатор: <code>{e(d.data.get('tenant_id', '—'))}</code>",
        "",
        e(d.data["description"][:1500] + ("…" if len(d.data["description"]) > 1500 else "")),
    ]
    return "\n".join(lines)


async def ask_current(ctx: AppContext, bot: Bot, chat_id: int, d: DraftView, user_id: int) -> None:
    """Отправляет вопрос текущего шага и сохраняет prompt_message_id условным UPDATE."""
    step = d.step
    if step in TEXT_STEPS:
        msg = await bot.send_message(
            chat_id, QUESTIONS[step], reply_markup=ForceReply(selective=True)
        )
        async with ctx.sm() as s, s.begin():
            # если черновик за это время изменился, prompt_message_id не перезаписывается
            await ctx.drafts.set_prompt_message_id(s, d.id, d.version, step, msg.message_id)
        if step == Step.TENANT_ID:
            await bot.send_message(
                chat_id,
                "Или пропустите шаг:",
                reply_markup=InlineKeyboardMarkup(
                    [[_btn("Пропустить", cb("s", d)), _btn("Отменить", cb("x", d))]]
                ),
            )
        return
    if step == Step.PRIORITY:
        rows = [
            [_btn(PRIORITY_LABELS[p.value], cb("p", d, p.value)) for p in list(Priority)[:2]],
            [_btn(PRIORITY_LABELS[p.value], cb("p", d, p.value)) for p in list(Priority)[2:]],
            [_btn("Отменить", cb("x", d))],
        ]
        await bot.send_message(chat_id, QUESTIONS[step], reply_markup=InlineKeyboardMarkup(rows))
        return
    # CONFIRM
    await bot.send_message(
        chat_id,
        summary(d),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [[_btn("Отправить", cb("ok", d)), _btn("Отменить", cb("x", d))]]
        ),
    )
