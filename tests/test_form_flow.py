"""Draft form end-to-end over handler cores + FakeGateway.

Covers the spec's tricky rules: prompt binding, re-ask without state change, stale and
foreign buttons, conditional prompt saves, atomic submit (ticket+draft delete in one tx),
double-submit races and post publication after commit.
"""

import asyncio

from sqlalchemy import select
from telegram import ForceReply, InlineKeyboardMarkup

from app.bot.handlers import create_form
from app.bot.handlers.common import Deps
from app.bot.handlers.create_form import draft_cb
from app.bot.handlers.router import route_message
from app.config import settings
from app.db.models import DraftStep, Priority, Role, Ticket, TicketDraft, User
from tests.fake_gateway import FakeGateway

AUTHOR = 801


def make_deps(session_factory) -> tuple[Deps, FakeGateway]:
    fake = FakeGateway()
    return Deps(gateway=fake, session_factory=session_factory, settings=settings), fake


async def mk_user(session_factory, telegram_id: int, role: Role | None = Role.support) -> None:
    async with session_factory() as s, s.begin():
        s.add(
            User(
                telegram_id=telegram_id,
                username=f"f{telegram_id}",
                full_name=f"Form {telegram_id}",
                role=role,
            )
        )


async def fetch_draft(session_factory, user_id: int) -> TicketDraft | None:
    async with session_factory() as s:
        return await s.scalar(select(TicketDraft).where(TicketDraft.user_id == user_id))


async def press(deps: Deps, user_id: int, data: str, callback_n: int = 1) -> None:
    await create_form.handle_draft_callback(
        deps,
        user_id=user_id,
        callback_id=f"cb{callback_n}",
        chat_id=user_id,
        draft_id=create_form.parse_draft_callback(data)[0],
        act=create_form.parse_draft_callback(data)[1],
        expected_version=create_form.parse_draft_callback(data)[2],
    )


async def start_form(deps: Deps, session_factory, user_id: int = AUTHOR):
    """Begin the form and return (draft, prompt_message_id)."""
    await create_form.handle_new(deps, user_id, user_id, f"f{user_id}", "Form User")
    draft = await fetch_draft(session_factory, user_id)
    assert draft is not None and draft.prompt_message_id is not None
    return draft, draft.prompt_message_id


async def answer(deps: Deps, *, user_id: int, text: str, reply_to: int) -> None:
    await create_form.handle_text(
        deps, user_id=user_id, chat_id=user_id, text=text, reply_to_message_id=reply_to
    )


async def test_happy_path_full_form(session_factory) -> None:
    await mk_user(session_factory, AUTHOR)
    deps, fake = make_deps(session_factory)

    draft, prompt = await start_form(deps, session_factory)
    assert draft.version == 1 and draft.step is DraftStep.description
    assert isinstance(fake.calls[-1].markup, ForceReply)

    await answer(deps, user_id=AUTHOR, text="Сломался лифт", reply_to=prompt)
    draft = await fetch_draft(session_factory, AUTHOR)
    assert draft is not None and draft.version == 2 and draft.step is DraftStep.priority

    await press(deps, AUTHOR, draft_cb(draft.id, "prio_h", 2))
    draft = await fetch_draft(session_factory, AUTHOR)
    assert draft is not None and draft.version == 3 and draft.priority is Priority.high

    await answer(deps, user_id=AUTHOR, text="RQ-42", reply_to=draft.prompt_message_id)
    draft = await fetch_draft(session_factory, AUTHOR)
    assert draft is not None and draft.version == 4 and draft.step is DraftStep.tenant_id

    await press(deps, AUTHOR, draft_cb(draft.id, "skip", 4))
    draft = await fetch_draft(session_factory, AUTHOR)
    assert draft is not None and draft.version == 5 and draft.step is DraftStep.confirm
    confirm_text = fake.calls[-1].text
    assert "Сломался лифт" in confirm_text and "RQ-42" in confirm_text and "Высокий" in confirm_text

    dev_before = sum(1 for c in fake.calls if c.chat_id == settings.dev_chat_id)
    await press(deps, AUTHOR, draft_cb(draft.id, "confirm", 5))

    # Ticket exists with the form's data; draft is gone; post published after commit.
    async with session_factory() as s:
        ticket = await s.scalar(select(Ticket).where(Ticket.author_id == AUTHOR))
    assert ticket is not None
    assert ticket.description == "Сломался лифт"
    assert ticket.priority is Priority.high and ticket.request_id == "RQ-42"
    assert ticket.tenant_id is None and ticket.version == 1
    assert await fetch_draft(session_factory, AUTHOR) is None
    assert fake.last_alert_text() == f"Создано обращение № {ticket.id}"
    dev_posts = [c for c in fake.calls[dev_before:] if c.chat_id == settings.dev_chat_id]
    assert dev_posts and f"№ {ticket.id}" in dev_posts[0].text
    assert ticket.message_id == dev_posts[0].message_id

    # Second «Отправить» (same button data): nothing is created.
    fake.calls.clear()
    await press(deps, AUTHOR, draft_cb(draft.id, "confirm", 5))
    async with session_factory() as s:
        count = len(list(await s.scalars(select(Ticket))))
    assert count == 1
    assert fake.last_alert_text() == create_form.CLOSED_ALERT
    assert not fake.calls  # no new ticket, no post traffic


async def test_double_confirm_race_creates_one_ticket(session_factory) -> None:
    await mk_user(session_factory, AUTHOR)
    deps, fake = make_deps(session_factory)
    draft, prompt = await start_form(deps, session_factory)
    await answer(deps, user_id=AUTHOR, text="Гонка", reply_to=prompt)
    draft = await fetch_draft(session_factory, AUTHOR)
    assert draft is not None
    await press(deps, AUTHOR, draft_cb(draft.id, "prio_m", 2))
    draft = await fetch_draft(session_factory, AUTHOR)
    assert draft is not None
    await answer(deps, user_id=AUTHOR, text="R-1", reply_to=draft.prompt_message_id)
    draft = await fetch_draft(session_factory, AUTHOR)
    assert draft is not None
    await press(deps, AUTHOR, draft_cb(draft.id, "skip", 4))
    draft = await fetch_draft(session_factory, AUTHOR)
    assert draft is not None

    data = draft_cb(draft.id, "confirm", 5)
    await asyncio.gather(*(press(deps, AUTHOR, data, i) for i in (1, 2)))

    async with session_factory() as s:
        tickets = list(await s.scalars(select(Ticket)))
    assert len(tickets) == 1
    alerts = {a.text for a in fake.alerts}
    assert any(t and t.startswith("Создано обращение") for t in alerts)
    assert create_form.CLOSED_ALERT in alerts


async def test_rejected_updates_do_not_change_state(session_factory) -> None:
    await mk_user(session_factory, AUTHOR)
    deps, fake = make_deps(session_factory)
    draft, _ = await start_form(deps, session_factory)

    # Answer to a stale prompt id: re-ask, no data/step/version change.
    await answer(deps, user_id=AUTHOR, text="не в тот вопрос", reply_to=999)
    draft = await fetch_draft(session_factory, AUTHOR)
    assert draft is not None and draft.version == 1 and draft.description is None

    # Foreign user pressing the button: alert, draft untouched.
    await mk_user(session_factory, 802, role=None)
    await press(deps, 802, draft_cb(draft.id, "prio_c", 1))
    assert fake.last_alert_text() == create_form.STALE_ALERT
    draft = await fetch_draft(session_factory, AUTHOR)
    assert draft is not None and draft.version == 1 and draft.priority is None

    # Stale version on a legit button: alert, no change.
    await press(deps, AUTHOR, draft_cb(draft.id, "prio_c", 1))
    assert fake.last_alert_text() == create_form.STALE_ALERT

    # Empty and overlong description: warning re-ask, state intact.
    current_prompt = draft.prompt_message_id
    assert current_prompt is not None
    await answer(deps, user_id=AUTHOR, text="   ", reply_to=current_prompt)
    old_prompt = (await fetch_draft(session_factory, AUTHOR)).prompt_message_id
    await answer(deps, user_id=AUTHOR, text="П" * 3001, reply_to=old_prompt)
    draft = await fetch_draft(session_factory, AUTHOR)
    assert draft is not None and draft.version == 1 and draft.description is None
    assert "не может быть пустым" in fake.calls[-2].text
    assert "длиннее 3000" in fake.calls[-1].text


async def test_resubmitted_answer_after_success_does_not_leak(session_factory) -> None:
    """A replayed description answer must not be written into the next field."""
    await mk_user(session_factory, AUTHOR)
    deps, _ = make_deps(session_factory)
    draft, prompt = await start_form(deps, session_factory)
    await answer(deps, user_id=AUTHOR, text="Сломался лифт", reply_to=prompt)
    draft = await fetch_draft(session_factory, AUTHOR)
    assert draft is not None and draft.version == 2

    # replay to old question
    await answer(deps, user_id=AUTHOR, text="Сломался лифт", reply_to=prompt)
    draft = await fetch_draft(session_factory, AUTHOR)
    assert draft is not None and draft.version == 2 and draft.request_id is None
    assert draft.step is DraftStep.priority  # only re-asked


async def test_resume_continue_restart(session_factory) -> None:
    await mk_user(session_factory, AUTHOR)
    deps, fake = make_deps(session_factory)
    draft, prompt = await start_form(deps, session_factory)
    fake.calls.clear()

    # /new with an active draft offers continue/restart, changes nothing.
    await create_form.handle_new(deps, AUTHOR, AUTHOR, "f801", "Form 801")
    markup = fake.calls[-1].markup
    assert isinstance(markup, InlineKeyboardMarkup)
    assert [b.text for b in markup.inline_keyboard[0]] == ["Продолжить", "Начать заново"]

    # continue: same version, fresh question.
    await press(deps, AUTHOR, draft_cb(draft.id, "continue", 1))
    again = await fetch_draft(session_factory, AUTHOR)
    assert again is not None and again.version == 1 and again.id == draft.id
    assert again.prompt_message_id != prompt  # new question message bound

    # restart: brand-new draft, old buttons become dead.
    await press(deps, AUTHOR, draft_cb(again.id, "restart", 1))
    fresh = await fetch_draft(session_factory, AUTHOR)
    assert fresh is not None and fresh.id != draft.id and fresh.version == 1
    await press(deps, AUTHOR, draft_cb(draft.id, "prio_c", 1))
    assert fake.last_alert_text() == create_form.CLOSED_ALERT


async def test_cancel(session_factory) -> None:
    await mk_user(session_factory, AUTHOR)
    deps, fake = make_deps(session_factory)
    await start_form(deps, session_factory)
    await create_form.handle_cancel(deps, AUTHOR, AUTHOR)
    assert "Анкета удалена" in fake.calls[-1].text
    assert await fetch_draft(session_factory, AUTHOR) is None
    await create_form.handle_cancel(deps, AUTHOR, AUTHOR)
    assert "нет" in fake.calls[-1].text.lower()


async def test_no_role_and_no_draft_interactions(session_factory) -> None:
    await mk_user(session_factory, 803, role=None)
    deps, fake = make_deps(session_factory)
    await create_form.handle_new(deps, 803, 803, "f803", "No Role")
    assert "админ" in fake.calls[-1].text.lower()
    assert await fetch_draft(session_factory, 803) is None
    # Text with no draft at all: silent, no sends.
    fake.calls.clear()
    await answer(deps, user_id=803, text="привет", reply_to=None)
    assert fake.calls == []


async def test_start_registers_and_bootstrap_admin(session_factory) -> None:
    deps, fake = make_deps(session_factory)
    await create_form.handle_start(deps, 1, 1, "rooty", "Root")  # BOOTSTRAP_ADMIN_IDS=[1]
    async with session_factory() as s:
        user = await s.get(User, 1)
    assert user is not None and user.role is Role.admin
    assert "admin" in fake.calls[-1].text

    await create_form.handle_start(deps, 804, 804, "newbie", "New B")
    async with session_factory() as s:
        user = await s.get(User, 804)
    assert user is not None and user.role is None
    assert "обратитесь" in fake.calls[-1].text.lower()


async def test_route_message_dispatches_command(session_factory) -> None:
    await mk_user(session_factory, AUTHOR)
    deps, fake = make_deps(session_factory)
    await route_message(
        deps,
        user_id=AUTHOR,
        chat_id=AUTHOR,
        username="f801",
        full_name="Form 801",
        text="/new",
        reply_to_message_id=None,
    )
    draft = await fetch_draft(session_factory, AUTHOR)
    assert draft is not None
    assert fake.calls[-1].kind == "send"


async def test_button_steps_reject_plain_text_replies(session_factory) -> None:
    """A text message at the priority step only re-asks the current question."""
    await mk_user(session_factory, AUTHOR)
    deps, fake = make_deps(session_factory)
    draft, prompt = await start_form(deps, session_factory)
    await answer(deps, user_id=AUTHOR, text="описание", reply_to=prompt)
    draft = await fetch_draft(session_factory, AUTHOR)
    assert draft is not None
    fake.calls.clear()
    await answer(deps, user_id=AUTHOR, text="высокий", reply_to=draft.prompt_message_id)
    assert fake.calls[-1].text.startswith(("Шаг 2/5", "⚠️"))
    after = await fetch_draft(session_factory, AUTHOR)
    assert after is not None and after.version == 2 and after.priority is None
