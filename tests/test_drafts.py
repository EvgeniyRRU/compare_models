import asyncio

import pytest
from sqlalchemy import func, select

from app.db.models import Ticket, User
from app.services.drafts import advance_button, advance_text, new_draft, save_prompt
from app.services.tickets import Conflict


async def test_replies_are_bound_to_current_prompt(sessions):
    async with sessions.begin() as session:
        user = User(telegram_id=1, name="Support", role="support")
        session.add(user)
        await session.flush()
        draft = await new_draft(session, user.id)
        uid, did, version = user.id, draft.id, draft.version
        await save_prompt(session, did, version, "description", 100)
    async with sessions.begin() as session:
        with pytest.raises(Conflict):
            await advance_text(session, uid, 99, "wrong")
        draft = await advance_text(session, uid, 100, "description")
        assert (draft.step, draft.version) == ("priority", 2)
        await save_prompt(session, did, version, "description", 101)
        assert draft.prompt_message_id is None
    async with sessions.begin() as session:
        with pytest.raises(Conflict):
            await advance_text(session, uid, 100, "again")
        with pytest.raises(Conflict):
            await advance_button(session, uid, did, 1, "high")


async def test_button_from_deleted_draft_cannot_change_replacement(sessions):
    async with sessions.begin() as session:
        user = User(telegram_id=1, name="Support", role="support")
        session.add(user)
        await session.flush()
        draft = await new_draft(session, user.id)
        uid, old_id = user.id, draft.id
        replacement = await new_draft(session, uid, restart=True)
        assert replacement.id != old_id
        with pytest.raises(Conflict):
            await advance_button(session, uid, old_id, 1, "high")


async def test_concurrent_replies_advance_once(sessions):
    async with sessions.begin() as session:
        user = User(telegram_id=1, name="Support", role="support")
        session.add(user)
        await session.flush()
        draft = await new_draft(session, user.id)
        uid = user.id
        await save_prompt(session, draft.id, draft.version, draft.step, 42)

    async def reply():
        try:
            async with sessions.begin() as session:
                await advance_text(session, uid, 42, "description")
            return True
        except Conflict:
            return False

    assert sorted(await asyncio.gather(reply(), reply())) == [False, True]


async def test_submit_is_atomic_and_cannot_be_repeated(sessions):
    async with sessions.begin() as session:
        user = User(telegram_id=1, name="Support", role="support")
        session.add(user)
        await session.flush()
        draft = await new_draft(session, user.id)
        uid, did = user.id, draft.id
        draft.step, draft.description, draft.priority, draft.request_id = (
            "confirm",
            "Issue",
            "high",
            "123",
        )
    async with sessions.begin() as session:
        ticket = await advance_button(session, uid, did, 1, "submit")
        assert ticket.id is not None
    async with sessions.begin() as session:
        with pytest.raises(Conflict):
            await advance_button(session, uid, did, 1, "submit")
        assert await session.scalar(select(func.count()).select_from(Ticket)) == 1
