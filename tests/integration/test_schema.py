# tests/integration/test_schema.py
import asyncio
import itertools
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models import User

# asyncpg не биндит строку в timestamp — только datetime-объекты.
_TS = datetime(2026, 1, 1, tzinfo=UTC)
# unique(telegram_id): каждый автор-тикета получает свой id.
_TG = itertools.count(10_000)


async def _make_user(session_factory, telegram_id: int | None = None) -> int:
    if telegram_id is None:
        telegram_id = next(_TG)
    async with session_factory() as s, s.begin():
        u = User(telegram_id=telegram_id, first_name="T")
        s.add(u)
        await s.flush()
        return u.id


async def _insert_ticket(session_factory, **kw) -> None:
    defaults = {
        "author_id": None,
        "assignee_id": None,
        "description": "x",
        "priority": "high",
        "request_id": "r1",
        "status": "new",
        "taken_at": None,
        "closed_at": None,
    }
    defaults["author_id"] = await _make_user(session_factory)
    defaults.update(kw)
    async with session_factory() as s, s.begin():
        await s.execute(
            text(
                "INSERT INTO tickets (author_id, assignee_id, description, priority, request_id, "
                "status, taken_at, closed_at) VALUES "
                "(:author_id, :assignee_id, :description, :priority, :request_id, "
                ":status, :taken_at, :closed_at)"
            ),
            defaults,
        )


async def test_new_cannot_have_assignee(db):
    with pytest.raises(IntegrityError):
        await _insert_ticket(db.session_factory, assignee_id=1, taken_at=_TS)


async def test_in_progress_requires_assignee(db):
    with pytest.raises(IntegrityError):
        await _insert_ticket(db.session_factory, status="in_progress", taken_at=_TS)


async def test_in_progress_requires_taken_at(db):
    uid = await _make_user(db.session_factory, telegram_id=7)
    with pytest.raises(IntegrityError):
        await _insert_ticket(db.session_factory, status="in_progress", assignee_id=uid)


async def test_final_requires_closed_at(db):
    with pytest.raises(IntegrityError):
        await _insert_ticket(db.session_factory, status="resolved", assignee_id=1, taken_at=_TS)


async def test_non_final_forbids_closed_at(db):
    with pytest.raises(IntegrityError):
        await _insert_ticket(db.session_factory, closed_at=_TS)


async def test_description_too_long(db):
    with pytest.raises(IntegrityError):
        await _insert_ticket(db.session_factory, description="y" * 3001)


async def test_description_max_ok(db):
    await _insert_ticket(db.session_factory, description="y" * 3000)


async def test_bad_priority(db):
    with pytest.raises(IntegrityError):
        await _insert_ticket(db.session_factory, priority="urgent")


async def test_ticket_numbers_are_sequential(db):
    await _insert_ticket(db.session_factory)
    await _insert_ticket(db.session_factory, request_id="r2")
    async with db.session_factory() as s:
        ids = (await s.execute(text("SELECT id FROM tickets ORDER BY id"))).scalars().all()
    assert ids == [1, 2]


async def test_one_draft_per_user(db):
    uid = await _make_user(db.session_factory)
    async with db.session_factory() as s, s.begin():
        await s.execute(
            text(
                "INSERT INTO ticket_drafts (user_id, step, version) VALUES (:u, 'description', 1)"
            ),
            {"u": uid},
        )
    with pytest.raises(IntegrityError):
        async with db.session_factory() as s, s.begin():
            await s.execute(
                text(
                    "INSERT INTO ticket_drafts (user_id, step, version) "
                    "VALUES (:u, 'description', 1)"
                ),
                {"u": uid},
            )


async def test_updated_at_changes_on_update(db):
    uid = await _make_user(db.session_factory)
    async with db.session_factory() as s:
        before = (
            await s.execute(text("SELECT updated_at FROM users WHERE id = :i"), {"i": uid})
        ).scalar_one()
    await asyncio.sleep(0.05)
    # ORM-обновление: onupdate=func.now() срабатывает только для ORM/Core-обновлений,
    # сырой SQL его не запускает (см. комментарий в app/models/base.py).
    async with db.session_factory() as s, s.begin():
        user = await s.get(User, uid)
        user.first_name = "X"
    async with db.session_factory() as s:
        after = (
            await s.execute(text("SELECT updated_at FROM users WHERE id = :i"), {"i": uid})
        ).scalar_one()
    assert after > before
