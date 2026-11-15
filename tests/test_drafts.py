"""Draft service primitives: conditional saves, prompt binding rules, TTL cleanup."""

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.db.models import DraftStep, TicketDraft, User
from app.services import drafts


async def mk_user(session, telegram_id: int) -> User:
    user = User(telegram_id=telegram_id, username=f"d{telegram_id}", full_name="Draft User")
    session.add(user)
    await session.commit()
    return user


async def mk_draft(session, user_id: int) -> TicketDraft:
    draft = await drafts.create_draft(session, user_id)
    await session.commit()
    return draft


async def test_create_and_fetch(db_session, session_factory) -> None:
    await mk_user(db_session, 701)
    draft = await mk_draft(db_session, 701)
    assert draft.version == 1 and draft.step is DraftStep.description
    fetched = await drafts.get_for_user(db_session, 701)
    assert fetched is not None and fetched.id == draft.id


async def test_one_active_draft_per_user(session_factory) -> None:
    async with session_factory() as s, s.begin():
        user = User(telegram_id=702, username="d702", full_name="Draft User")
        s.add(user)
    async with session_factory() as s, s.begin():
        await drafts.create_draft(s, 702)
    with pytest.raises(IntegrityError):  # at most one active draft per user
        async with session_factory() as s, s.begin():
            await drafts.create_draft(s, 702)


async def test_save_answer_advances_version_and_step(db_session) -> None:
    await mk_user(db_session, 703)
    draft = await mk_draft(db_session, 703)
    ok = await drafts.save_answer(
        db_session,
        draft.id,
        expected_version=1,
        expected_step=DraftStep.description,
        field_values={"description": "текст"},
        new_step=DraftStep.priority,
    )
    assert ok
    await db_session.commit()
    fresh = await db_session.get(TicketDraft, draft.id)
    await db_session.refresh(fresh)
    assert fresh is not None and fresh.version == 2 and fresh.step is DraftStep.priority
    assert fresh.description == "текст"


async def test_save_answer_stale_version_changes_nothing(db_session) -> None:
    await mk_user(db_session, 704)
    draft = await mk_draft(db_session, 704)
    await drafts.save_answer(
        db_session,
        draft.id,
        expected_version=1,
        expected_step=DraftStep.description,
        field_values={"description": "первый"},
        new_step=DraftStep.priority,
    )
    await db_session.commit()
    stale = await drafts.save_answer(
        db_session,
        draft.id,
        expected_version=1,
        expected_step=DraftStep.description,
        field_values={"description": "СТАРЫЙ"},
        new_step=DraftStep.priority,
    )
    assert stale is False
    await db_session.commit()
    fresh = await db_session.get(TicketDraft, draft.id)
    await db_session.refresh(fresh)
    assert fresh is not None and fresh.version == 2 and fresh.description == "первый"


async def test_save_answer_wrong_step_changes_nothing(db_session) -> None:
    await mk_user(db_session, 705)
    draft = await mk_draft(db_session, 705)
    ok = await drafts.save_answer(
        db_session,
        draft.id,
        expected_version=1,
        expected_step=DraftStep.priority,  # draft is on description
        field_values={"priority": None},
        new_step=DraftStep.request_id,
    )
    assert ok is False
    draft_id = draft.id
    await db_session.rollback()
    fresh = await db_session.get(TicketDraft, draft_id)
    assert fresh is not None and fresh.version == 1 and fresh.step is DraftStep.description


async def test_set_prompt_only_for_matching_version_step(db_session, session_factory) -> None:
    await mk_user(db_session, 706)
    draft = await mk_draft(db_session, 706)
    # Late send for the (v1, description) question while draft already moved to v2.
    await drafts.save_answer(
        db_session,
        draft.id,
        expected_version=1,
        expected_step=DraftStep.description,
        field_values={"description": "x"},
        new_step=DraftStep.priority,
    )
    await db_session.commit()
    late = await drafts.set_prompt(
        session_factory, draft.id, version=1, step=DraftStep.description, message_id=5001
    )
    assert late is False
    fresh = await db_session.get(TicketDraft, draft.id)
    assert fresh is not None and fresh.prompt_message_id is None
    current = await drafts.set_prompt(
        session_factory, draft.id, version=2, step=DraftStep.priority, message_id=5002
    )
    assert current is True
    await db_session.refresh(fresh)
    assert fresh.prompt_message_id == 5002


async def test_cleanup_expired_drafts(db_session, session_factory) -> None:
    await mk_user(db_session, 707)
    old = await mk_draft(db_session, 707)
    fresh_user = User(telegram_id=708, username="d708", full_name="Fresh")
    db_session.add(fresh_user)
    await db_session.commit()
    fresh = await mk_draft(db_session, 708)
    async with db_session.begin():
        await db_session.execute(
            text(
                "UPDATE ticket_drafts SET created_at = now() - interval '25 hours' WHERE id = :did"
            ),
            {"did": old.id},
        )
    removed = await drafts.cleanup_expired(session_factory, 24)
    assert removed == 1
    remaining = await db_session.scalars(select(TicketDraft))
    assert [d.id for d in remaining] == [fresh.id]


async def test_lock_returns_fresh_row(session_factory) -> None:
    async with session_factory() as s, s.begin():
        user = User(telegram_id=709, username="d709", full_name="Draft User")
        s.add(user)
    async with session_factory() as s, s.begin():
        draft = await drafts.create_draft(s, 709)
    async with session_factory() as s, s.begin():
        locked = await drafts.lock(s, draft.id)
        assert locked is not None and locked.version == 1
