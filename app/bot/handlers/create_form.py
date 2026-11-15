"""Draft form core: commands, bound text answers, form buttons.

Rules implemented from the spec (анкета):
- steps description -> priority -> request_id -> tenant_id -> confirm;
- ≤1 active draft per user; text answers bind to prompt_message_id;
- rejected/stale updates never touch data, step or version;
- prompt ids are saved conditionally by (version, step) after the send, with no
  transaction open across Telegram calls;
- «Отправить» creates the ticket and deletes the draft in one transaction;
  the post is published after commit through post-sync.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.handlers.common import Deps, toast
from app.bot.render import PRIORITY_LABELS
from app.db.models import DraftStep, Priority, TicketDraft
from app.logging_setup import get_logger
from app.services import drafts as drafts_svc
from app.services import tickets as tickets_svc
from app.services import users as users_svc
from app.services.post_sync import sync_ticket
from app.telegram_gateway import ReplyMarkup

log = get_logger("bot.form")

MAX_DESC = 3000
MAX_ID = 64

RESUME_ALERT = "Продолжите с актуального сообщения: /new"
STALE_ALERT = "Кнопка устарела или от другой анкеты. Продолжите с актуального сообщения"
CLOSED_ALERT = "Анкета уже закрыта. Начните заново: /new"

PRIO_ACTS = {
    "prio_c": Priority.critical,
    "prio_h": Priority.high,
    "prio_m": Priority.medium,
    "prio_l": Priority.low,
}


def draft_cb(draft_id: UUID, act: str, version: int) -> str:
    return f"d:{draft_id.hex}:{act}:{version}"


def parse_draft_callback(data: str) -> tuple[UUID, str, int]:
    """'d:<hex32>:<act>:<version>' -> parts; ValueError on garbage."""
    parts = data.split(":")
    if len(parts) != 4 or parts[0] != "d":
        raise ValueError(f"bad draft callback: {data!r}")
    return UUID(parts[1]), parts[2], int(parts[3])


def _markup(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(text, callback_data=cb) for text, cb in row] for row in rows]
    )


def priority_markup(draft_id: UUID, version: int) -> InlineKeyboardMarkup:
    return _markup(
        [
            [
                ("Критический", draft_cb(draft_id, "prio_c", version)),
                ("Высокий", draft_cb(draft_id, "prio_h", version)),
            ],
            [
                ("Средний", draft_cb(draft_id, "prio_m", version)),
                ("Низкий", draft_cb(draft_id, "prio_l", version)),
            ],
        ]
    )


def resume_markup(draft_id: UUID, version: int) -> InlineKeyboardMarkup:
    return _markup(
        [
            [
                ("Продолжить", draft_cb(draft_id, "continue", version)),
                ("Начать заново", draft_cb(draft_id, "restart", version)),
            ]
        ]
    )


def skip_markup(draft_id: UUID, version: int) -> InlineKeyboardMarkup:
    return _markup([[("Пропустить", draft_cb(draft_id, "skip", version))]])


def confirm_markup(draft_id: UUID, version: int) -> InlineKeyboardMarkup:
    return _markup(
        [
            [
                ("Отправить", draft_cb(draft_id, "confirm", version)),
                ("Отменить", draft_cb(draft_id, "cancel", version)),
            ]
        ]
    )


def _confirm_text(
    description: str,
    priority: Priority,
    request_id: str,
    tenant_id: str | None,
) -> str:
    lines = [
        "Шаг 5/5. Проверьте обращение:",
        "",
        f"Описание: {description}",
        f"Приоритет: {PRIORITY_LABELS[priority]}",
        f"ID заявки: {request_id}",
        f"ID арендатора: {tenant_id or '—'}",
        "",
        "Отправить или отменить:",
    ]
    return "\n".join(lines)[:4096]


async def ask(
    deps: Deps,
    *,
    chat_id: int,
    draft_id: UUID,
    version: int,
    step: DraftStep,
    warning: str | None = None,
    summary: tuple[str, Priority, str, str | None] | None = None,
) -> None:
    """Send the question for (draft_id, version, step) and save its prompt id conditionally."""
    prefix = f"⚠️ {warning}\n\n" if warning else ""
    reply_markup: ReplyMarkup | None = None
    force_reply = False
    if step is DraftStep.description:
        text = f"{prefix}Шаг 1/5. Опишите проблему (до {MAX_DESC} символов):"
        force_reply = True
    elif step is DraftStep.priority:
        text = f"{prefix}Шаг 2/5. Выберите приоритет:"
        reply_markup = priority_markup(draft_id, version)
    elif step is DraftStep.request_id:
        text = f"{prefix}Шаг 3/5. Введите ID заявки (до {MAX_ID} символов):"
        force_reply = True
    elif step is DraftStep.tenant_id:
        text = (
            f"{prefix}Шаг 4/5. Введите ID арендатора (до {MAX_ID} символов) или пропустите кнопкой:"
        )
        reply_markup = skip_markup(draft_id, version)
    else:  # confirm
        if summary is None:
            raise ValueError("confirm step requires summary")
        text = _confirm_text(*summary)
        reply_markup = confirm_markup(draft_id, version)
    message_id = await deps.gateway.send_message(
        chat_id,
        text,
        reply_markup=ForceReply() if force_reply else reply_markup,
    )
    await drafts_svc.set_prompt(
        deps.session_factory, draft_id, version=version, step=step, message_id=message_id
    )


async def handle_start(
    deps: Deps, user_id: int, chat_id: int, username: str | None, full_name: str | None
) -> None:
    async with deps.session_factory() as session:
        async with session.begin():
            user = await users_svc.ensure_user(session, user_id, username, full_name)
            user = await users_svc.apply_bootstrap_role(session, user, deps.settings)
        role = user.role
    if role is None:
        await deps.gateway.send_message(
            chat_id,
            "Вы зарегистрированы. Доступ к боту выдаёт администратор — обратитесь к админу.",
        )
    else:
        await deps.gateway.send_message(
            chat_id,
            f"Ваша роль: {role.value}. /new — создать обращение, /help — справка.",
        )


HELP_TEXT = (
    "Команды:\n"
    "/new — создать обращение (анкета)\n"
    "/cancel — отменить текущую анкету\n"
    "/help — эта справка\n"
    "Кнопка меню — список обращений (Mini App)."
)


async def handle_help(deps: Deps, chat_id: int) -> None:
    await deps.gateway.send_message(chat_id, HELP_TEXT)


async def handle_cancel(deps: Deps, user_id: int, chat_id: int) -> None:
    async with deps.session_factory() as session, session.begin():
        draft = await drafts_svc.get_for_user_locked(session, user_id)
        if draft is not None:
            await drafts_svc.delete_draft(session, draft.id)
    await deps.gateway.send_message(
        chat_id, "Анкета удалена." if draft is not None else "Активной анкеты нет."
    )


async def handle_new(
    deps: Deps, user_id: int, chat_id: int, username: str | None, full_name: str | None
) -> None:
    async with deps.session_factory() as session:
        async with session.begin():
            user = await users_svc.ensure_user(session, user_id, username, full_name)
            user = await users_svc.apply_bootstrap_role(session, user, deps.settings)
        if user.role is None:
            await deps.gateway.send_message(
                chat_id, "Доступ к боту выдаёт администратор — обратитесь к админу."
            )
            return
    async with deps.session_factory() as session, session.begin():
        existing = await drafts_svc.get_for_user_locked(session, user_id)
        if existing is not None:
            snapshot = (existing.id, existing.version)
        else:
            created = await drafts_svc.create_draft(session, user_id)
            snapshot = (created.id, created.version)
    if existing is not None:
        await deps.gateway.send_message(
            chat_id,
            "У вас есть незаполненная анкета. Продолжить или начать заново?",
            reply_markup=resume_markup(snapshot[0], snapshot[1]),
        )
        return
    await ask(
        deps,
        chat_id=chat_id,
        draft_id=snapshot[0],
        version=snapshot[1],
        step=DraftStep.description,
    )


@dataclass
class _AfterTx:
    """What to do with Telegram after the draft transaction commits."""

    kind: str = "none"  # none | ask | sync | toast
    step: DraftStep | None = None
    version: int = 0
    summary: tuple[str, Priority, str, str | None] | None = None
    warning: str | None = None
    message: str | None = None
    alert: bool = False
    ticket_id: int | None = None


async def handle_draft_callback(
    deps: Deps,
    *,
    user_id: int,
    callback_id: str,
    chat_id: int,
    draft_id: UUID,
    act: str,
    expected_version: int,
) -> None:
    """One transaction locks+validates+applies; every Telegram call happens after commit."""
    draft_id_out = draft_id
    after = _AfterTx()
    async with deps.session_factory() as session, session.begin():
        draft = await drafts_svc.lock(session, draft_id)
        if draft is None:
            after = _AfterTx(kind="toast", message=CLOSED_ALERT, alert=True)
        elif draft.user_id != user_id or draft.version != expected_version:
            after = _AfterTx(kind="toast", message=STALE_ALERT, alert=True)
        elif act == "continue":
            after = _AfterTx(
                kind="ask",
                step=draft.step,
                version=draft.version,
                summary=_summary_of(draft),
            )
        elif act == "restart":
            await drafts_svc.delete_draft(session, draft_id)
            fresh = await drafts_svc.create_draft(session, user_id)
            draft_id_out = fresh.id
            after = _AfterTx(kind="ask", step=fresh.step, version=fresh.version)
        elif act in PRIO_ACTS:
            after, changed = await _advance(
                session,
                draft,
                want_step=DraftStep.priority,
                field_values={"priority": PRIO_ACTS[act]},
                new_step=DraftStep.request_id,
                expected_version=expected_version,
            )
            if not changed:
                after = _AfterTx(kind="toast", message=STALE_ALERT, alert=True)
        elif act == "skip":
            if draft.step is not DraftStep.tenant_id:
                after = _AfterTx(kind="toast", message=STALE_ALERT, alert=True)
            else:
                summary = _summary_of(draft, tenant_id=None)
                after, changed = await _advance(
                    session,
                    draft,
                    want_step=DraftStep.tenant_id,
                    field_values={"tenant_id": None},
                    new_step=DraftStep.confirm,
                    expected_version=expected_version,
                    summary=summary,
                )
                if not changed:
                    after = _AfterTx(kind="toast", message=STALE_ALERT, alert=True)
        elif act == "cancel":
            await drafts_svc.delete_draft(session, draft_id)
            after = _AfterTx(kind="toast", message="Анкета отменена")
        elif act == "confirm":
            if draft.step is not DraftStep.confirm:
                after = _AfterTx(kind="toast", message=STALE_ALERT, alert=True)
            else:
                ticket = await tickets_svc.create(
                    session,
                    deps.settings,
                    author_id=user_id,
                    description=draft.description or "",
                    priority=str(draft.priority),
                    request_id=draft.request_id or "",
                    tenant_id=draft.tenant_id,
                )
                await drafts_svc.delete_draft(session, draft_id)
                after = _AfterTx(
                    kind="sync",
                    ticket_id=ticket.id,
                    message=f"Создано обращение № {ticket.id}",
                )
        else:
            after = _AfterTx(kind="toast", message="Кнопка не распознана", alert=True)

    await _execute_after(
        deps, after, callback_id=callback_id, chat_id=chat_id, draft_id=draft_id_out
    )


def _summary_of(
    draft: TicketDraft, *, tenant_id: str | None = "keep"
) -> tuple[str, Priority, str, str | None]:
    return (
        draft.description or "",
        draft.priority or Priority.medium,
        draft.request_id or "",
        draft.tenant_id if tenant_id == "keep" else tenant_id,
    )


async def _advance(
    session: AsyncSession,
    draft: TicketDraft,
    *,
    want_step: DraftStep,
    field_values: dict[str, object],
    new_step: DraftStep,
    expected_version: int,
    summary: tuple[str, Priority, str, str | None] | None = None,
) -> tuple[_AfterTx, bool]:
    if draft.step is not want_step:
        return _AfterTx(), False
    ok = await drafts_svc.save_answer(
        session,
        draft.id,
        expected_version=expected_version,
        expected_step=want_step,
        field_values=field_values,
        new_step=new_step,
    )
    if not ok:
        return _AfterTx(), False
    return _AfterTx(kind="ask", step=new_step, version=expected_version + 1, summary=summary), True


async def _execute_after(
    deps: Deps, after: _AfterTx, *, callback_id: str, chat_id: int, draft_id: UUID
) -> None:
    if after.kind == "toast" and after.message:
        await toast(deps.gateway, callback_id, after.message, alert=after.alert)
    elif after.kind == "ask" and after.step is not None:
        if after.message:
            await toast(deps.gateway, callback_id, after.message, alert=after.alert)
        await ask(
            deps,
            chat_id=chat_id,
            draft_id=draft_id,
            version=after.version,
            step=after.step,
            warning=after.warning,
            summary=after.summary if after.step is DraftStep.confirm else None,
        )
    elif after.kind == "sync" and after.ticket_id is not None:
        if after.message:
            await toast(deps.gateway, callback_id, after.message, alert=False)
        # Publish after commit; a failure there must not lose the ticket (sweeper retries).
        try:
            await sync_ticket(deps.gateway, deps.session_factory, deps.settings, after.ticket_id)
        except Exception:
            log.exception("form post publish failed", component="form", ticket_id=after.ticket_id)


def _validate_text(step: DraftStep, raw: str) -> tuple[str | None, str | None]:
    """Return (value, error). Empty tenant answer is not an error at ask level."""
    value = raw.strip()
    if step is DraftStep.description:
        if not value:
            return None, "Описание не может быть пустым"
        if len(value) > MAX_DESC:
            return None, f"Описание длиннее {MAX_DESC} символов"
        return value, None
    if not value:
        return None, "Значение не может быть пустым (ID арендатора можно пропустить кнопкой)"
    if len(value) > MAX_ID:
        return None, f"Значение длиннее {MAX_ID} символов"
    return value, None


async def handle_text(
    deps: Deps, *, user_id: int, chat_id: int, text: str, reply_to_message_id: int | None
) -> None:
    """Bound text answers; everything unbound/invalid just re-asks without state change."""
    after = _AfterTx()
    async with deps.session_factory() as session, session.begin():
        draft = await drafts_svc.get_for_user_locked(session, user_id)
        if draft is None:
            return
        step = draft.step
        version = draft.version
        draft_id = draft.id
        if step not in drafts_svc.TEXT_STEPS:
            after = _AfterTx(
                kind="ask",
                step=step,
                version=version,
                summary=_summary_of(draft),
                warning="Ответьте на актуальный вопрос анкеты",
            )
        elif reply_to_message_id is None or reply_to_message_id != draft.prompt_message_id:
            after = _AfterTx(
                kind="ask",
                step=step,
                version=version,
                warning="Ответьте на актуальный вопрос анкеты",
            )
        else:
            value, error = _validate_text(step, text)
            if error is not None:
                after = _AfterTx(kind="ask", step=step, version=version, warning=error)
            else:
                assert value is not None
                ok = await drafts_svc.save_answer(
                    session,
                    draft_id,
                    expected_version=version,
                    expected_step=step,
                    field_values={step.value: value},
                    new_step=drafts_svc.NEXT_STEP[step],
                )
                if not ok:
                    after = _AfterTx(
                        kind="ask",
                        step=step,
                        version=version,
                        warning="Ответьте на актуальный вопрос анкеты",
                    )
                else:
                    next_step = drafts_svc.NEXT_STEP[step]
                    summary = _summary_of(draft)
                    if step is DraftStep.description:
                        summary = (value, summary[1], summary[2], summary[3])
                    elif step is DraftStep.request_id:
                        summary = (summary[0], summary[1], value, summary[3])
                    elif step is DraftStep.tenant_id:
                        summary = (summary[0], summary[1], summary[2], value)
                    after = _AfterTx(
                        kind="ask", step=next_step, version=version + 1, summary=summary
                    )

    assert after.kind == "ask" and after.step is not None
    await ask(
        deps,
        chat_id=chat_id,
        draft_id=draft_id,
        version=after.version,
        step=after.step,
        warning=after.warning,
        summary=after.summary if after.step is DraftStep.confirm else None,
    )
