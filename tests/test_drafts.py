import asyncio
from datetime import timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from support_bot.config import Settings
from support_bot.db.models import Ticket, TicketDraft
from support_bot.services import drafts as svc
from tests.helpers import NOW, add_user

TTL = 24


async def _start(session: AsyncSession, user_id: int) -> svc.DraftState:
    state = await svc.start(session, user_id, now=NOW, ttl_hours=TTL)
    await session.commit()
    return state


async def _answer(session: AsyncSession, user: int, reply_to: int | None, text: str,
                  now=NOW) -> svc.Outcome:
    outcome = await svc.answer_text(
        session, user_id=user, reply_to_message_id=reply_to, text=text, now=now, ttl_hours=TTL
    )
    await session.commit()
    return outcome


async def _press(session: AsyncSession, user: int, state: svc.DraftState, action: str,
                 value: str | None = None, *, version: int | None = None,
                 draft_id: UUID | None = None, settings: Settings) -> svc.Outcome:
    outcome = await svc.press(
        session,
        user_id=user,
        draft_id=draft_id or state.draft_id,
        version=state.version if version is None else version,
        action=action,
        value=value,
        now=NOW,
        ttl_hours=TTL,
        settings=settings,
    )
    await session.commit()
    return outcome


async def _bind_prompt(session: AsyncSession, state: svc.DraftState, message_id: int) -> None:
    ok = await svc.record_prompt(
        session, draft_id=state.draft_id, version=state.version, step=state.step,
        message_id=message_id, now=NOW,
    )
    await session.commit()
    assert ok


async def _fill_until_confirm(session: AsyncSession, user: int, settings: Settings) -> svc.DraftState:
    state = await _start(session, user)
    await _bind_prompt(session, state, 1)
    out = await _answer(session, user, 1, "Не работает отчёт")
    assert out.kind == "advanced" and out.state is not None
    await _bind_prompt(session, out.state, 2)
    out = await _press(session, user, out.state, "priority", "high", settings=settings)
    assert out.kind == "advanced" and out.state is not None
    await _bind_prompt(session, out.state, 3)
    out = await _answer(session, user, 3, "REQ-42")
    assert out.kind == "advanced" and out.state is not None
    await _bind_prompt(session, out.state, 4)
    out = await _press(session, user, out.state, "skip", settings=settings)
    assert out.kind == "advanced" and out.state is not None
    assert out.state.step == "confirm"
    return out.state


async def test_full_form_creates_one_ticket_and_removes_draft(
    session: AsyncSession, settings: Settings
) -> None:
    user = await add_user(session, 1, "support")
    state = await _fill_until_confirm(session, user, settings)
    await _bind_prompt(session, state, 9)

    out = await _press(session, user, state, "confirm", settings=settings)

    assert out.kind == "created" and out.ticket_id is not None
    ticket = await session.get(Ticket, out.ticket_id)
    assert ticket is not None
    assert ticket.description == "Не работает отчёт"
    assert ticket.priority == "high"
    assert ticket.request_id == "REQ-42"
    assert ticket.tenant_id is None
    assert (await session.scalars(select(TicketDraft))).all() == []


async def test_each_successful_step_bumps_version_exactly_once(
    session: AsyncSession, settings: Settings
) -> None:
    user = await add_user(session, 2, "support")
    state = await _start(session, user)
    assert state.version == 1
    await _bind_prompt(session, state, 1)

    out = await _answer(session, user, 1, "описание")

    assert out.state is not None
    assert out.state.version == 2
    assert out.state.step == "priority"


async def test_reply_not_bound_to_current_prompt_changes_nothing(
    session: AsyncSession, settings: Settings
) -> None:
    user = await add_user(session, 3, "support")
    state = await _start(session, user)
    await _bind_prompt(session, state, 10)

    no_reply = await _answer(session, user, None, "текст без reply")
    other = await _answer(session, user, 99, "reply на другой вопрос")

    assert no_reply.kind == "reprompt" and other.kind == "reprompt"
    row = await session.scalar(select(TicketDraft))
    assert row is not None
    assert row.version == 1 and row.step == "description" and row.description is None


async def test_reply_without_bound_prompt_is_not_accepted(
    session: AsyncSession, settings: Settings
) -> None:
    user = await add_user(session, 4, "support")
    await _start(session, user)

    out = await _answer(session, user, 5, "ответ")

    assert out.kind == "reprompt"
    row = await session.scalar(select(TicketDraft))
    assert row is not None and row.description is None


async def test_late_prompt_send_does_not_overwrite_next_step_prompt(
    session: AsyncSession, settings: Settings
) -> None:
    user = await add_user(session, 5, "support")
    state = await _start(session, user)
    await _bind_prompt(session, state, 20)
    out = await _answer(session, user, 20, "описание")
    assert out.state is not None
    await _bind_prompt(session, out.state, 21)

    # a send of the step-1 question that finished late must not replace the step-2 prompt
    ok = await svc.record_prompt(
        session, draft_id=state.draft_id, version=state.version, step=state.step,
        message_id=999, now=NOW,
    )
    await session.commit()

    assert ok is False
    row = await session.scalar(select(TicketDraft))
    assert row is not None and row.prompt_message_id == 21


async def test_invalid_text_keeps_step_and_version(
    session: AsyncSession, settings: Settings
) -> None:
    user = await add_user(session, 6, "support")
    state = await _start(session, user)
    await _bind_prompt(session, state, 30)

    out = await _answer(session, user, 30, "   ")

    assert out.kind == "invalid" and out.message
    row = await session.scalar(select(TicketDraft))
    assert row is not None and row.version == 1 and row.step == "description"


async def test_concurrent_replies_to_one_prompt_apply_once(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    user = await add_user(session, 7, "support")
    state = await _start(session, user)
    await _bind_prompt(session, state, 40)

    async def reply(text: str) -> svc.Outcome:
        async with session_factory() as s:
            return await _answer(s, user, 40, text)

    outcomes = await asyncio.gather(reply("первый"), reply("второй"), reply("третий"))

    assert sorted(o.kind for o in outcomes) == ["advanced", "reprompt", "reprompt"]
    row = await session.scalar(select(TicketDraft))
    assert row is not None and row.version == 2 and row.description in {"первый", "второй", "третий"}


async def test_button_from_deleted_draft_cannot_confirm_new_one(
    session: AsyncSession, settings: Settings
) -> None:
    user = await add_user(session, 8, "support")
    old = await _fill_until_confirm(session, user, settings)
    await svc.cancel(session, user)
    await session.commit()
    await session.commit()
    await svc.start(session, user, now=NOW, ttl_hours=TTL)
    await session.commit()

    out = await _press(session, user, old, "confirm", settings=settings)

    assert out.kind == "rejected"
    assert (await session.scalars(select(Ticket))).all() == []


async def test_button_with_stale_version_is_rejected(
    session: AsyncSession, settings: Settings
) -> None:
    user = await add_user(session, 9, "support")
    state = await _start(session, user)
    await _bind_prompt(session, state, 50)
    await _answer(session, user, 50, "описание")  # now version 2, step priority

    out = await _press(session, user, state, "priority", "low", settings=settings)

    assert out.kind == "rejected"
    row = await session.scalar(select(TicketDraft))
    assert row is not None and row.priority is None and row.version == 2


async def test_foreign_user_cannot_press_buttons_of_someone_elses_draft(
    session: AsyncSession, settings: Settings
) -> None:
    owner = await add_user(session, 10, "support")
    intruder = await add_user(session, 11, "support")
    state = await _start(session, owner)
    await _bind_prompt(session, state, 60)
    await _answer(session, owner, 60, "описание")

    out = await _press(session, intruder, state, "priority", "critical", settings=settings)

    assert out.kind == "rejected"


async def test_double_confirm_creates_single_ticket(
    session: AsyncSession, settings: Settings
) -> None:
    user = await add_user(session, 12, "support")
    state = await _fill_until_confirm(session, user, settings)

    first = await _press(session, user, state, "confirm", settings=settings)
    second = await _press(session, user, state, "confirm", settings=settings)

    assert first.kind == "created"
    assert second.kind == "rejected"
    assert len((await session.scalars(select(Ticket))).all()) == 1


async def test_expired_draft_is_not_continued_and_new_start_replaces_it(
    session: AsyncSession, settings: Settings
) -> None:
    user = await add_user(session, 13, "support")
    state = await _start(session, user)
    await _bind_prompt(session, state, 70)

    later = NOW + timedelta(hours=TTL + 1)
    out = await svc.answer_text(
        session, user_id=user, reply_to_message_id=70, text="поздно", now=later, ttl_hours=TTL
    )
    await session.commit()

    assert out.kind == "rejected"
    assert await svc.get_active(session, user, now=later, ttl_hours=TTL) is None


async def test_only_one_active_draft_per_user(session: AsyncSession, settings: Settings) -> None:
    user = await add_user(session, 14, "support")
    first = await _start(session, user)

    existing = await svc.get_active(session, user, now=NOW, ttl_hours=TTL)

    assert existing is not None and existing.draft_id == first.draft_id


async def test_restart_replaces_draft_with_new_id(session: AsyncSession, settings: Settings) -> None:
    user = await add_user(session, 15, "support")
    first = await _start(session, user)

    fresh = await svc.restart(session, user, now=NOW)
    await session.commit()

    assert fresh.draft_id != first.draft_id
    assert fresh.version == 1 and fresh.step == "description"


async def test_cancel_at_any_step_removes_draft(session: AsyncSession, settings: Settings) -> None:
    user = await add_user(session, 16, "support")
    state = await _fill_until_confirm(session, user, settings)

    out = await _press(session, user, state, "cancel", settings=settings)

    assert out.kind == "cancelled"
    assert (await session.scalars(select(TicketDraft))).all() == []


async def test_draft_callbacks_fit_telegram_limit(settings: Settings) -> None:
    from uuid import uuid4

    data = svc.draft_callback("pr", uuid4(), 123_456, "critical")

    assert len(data.encode()) <= 64
    assert svc.parse_draft_callback(data) is not None


async def test_draft_ttl_is_checked_on_press(session: AsyncSession, settings: Settings, engine: AsyncEngine) -> None:
    user = await add_user(session, 17, "support")
    state = await _start(session, user)
    later = NOW + timedelta(hours=TTL + 1)

    out = await svc.press(
        session, user_id=user, draft_id=state.draft_id, version=state.version,
        action="cancel", value=None, now=later, ttl_hours=TTL, settings=settings,
    )
    await session.commit()

    assert out.kind == "rejected"
