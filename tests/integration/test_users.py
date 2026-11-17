# tests/integration/test_users.py
import asyncio

import pytest
from sqlalchemy import text

from app.services import tickets as tickets_svc
from app.services import users as users_svc
from app.services.errors import ActiveTicketsError, PermissionDenied


async def test_upsert_creates_then_updates(db):
    async with db.session_factory() as s, s.begin():
        u = await users_svc.upsert_user(s, telegram_id=1, username="a", first_name="A")
        uid = u.id
    async with db.session_factory() as s, s.begin():
        u = await users_svc.upsert_user(s, telegram_id=1, username="a2", first_name="A2")
        assert u.id == uid
    async with db.session_factory() as s:
        row = (
            await s.execute(
                text("SELECT username, first_name FROM users WHERE id = :i"), {"i": uid}
            )
        ).one()
    assert row == ("a2", "A2")


async def test_change_role_grant_and_clear(db):
    async with db.session_factory() as s, s.begin():
        u = await users_svc.upsert_user(s, telegram_id=1)
        await users_svc.change_role(s, user_id=u.id, new_role="developer")
        await users_svc.change_role(s, user_id=u.id, new_role=None)
    async with db.session_factory() as s:
        role = (await s.execute(text("SELECT role FROM users WHERE telegram_id = 1"))).scalar_one()
    assert role is None


async def test_change_role_invalid(db):
    async with db.session_factory() as s, s.begin():
        u = await users_svc.upsert_user(s, telegram_id=1)
        with pytest.raises(ValueError):
            await users_svc.change_role(s, user_id=u.id, new_role="boss")


async def test_change_role_forbidden_with_active_tickets(db, settings):
    async with db.session_factory() as s, s.begin():
        sup = await users_svc.upsert_user(s, telegram_id=1, first_name="Sup")
        dev = await users_svc.upsert_user(s, telegram_id=2, first_name="Dev")
        await users_svc.change_role(s, user_id=dev.id, new_role="developer")
        t = await tickets_svc.create_ticket(
            s, settings, sup.id, description="x", priority="high", request_id="r1"
        )
        await tickets_svc.take_ticket(s, settings, dev.id, t, expected_version=t.version)
        dev_id, ticket_id = dev.id, t.id
    with pytest.raises(ActiveTicketsError) as exc_info:
        async with db.session_factory() as s, s.begin():
            await users_svc.change_role(s, user_id=dev_id, new_role="support")
    assert exc_info.value.ticket_ids == [ticket_id]
    async with db.session_factory() as s:
        role = (
            await s.execute(text("SELECT role FROM users WHERE id = :i"), {"i": dev_id})
        ).scalar_one()
    assert role == "developer"  # транзакция откатилась


async def test_role_change_and_take_are_serialized(db, settings):
    """FOR UPDATE (смена роли) и FOR SHARE (взятие) исключают взаимно:
    взятие, стартовавшее параллельно со снятием роли, должно отклониться."""
    async with db.session_factory() as s, s.begin():
        sup = await users_svc.upsert_user(s, telegram_id=1)
        dev = await users_svc.upsert_user(s, telegram_id=2)
        await users_svc.change_role(s, user_id=dev.id, new_role="developer")
        t = await tickets_svc.create_ticket(
            s, settings, sup.id, description="x", priority="high", request_id="r1"
        )
        dev_id, ticket_id = dev.id, t.id

    async def role_change():
        async with db.session_factory() as s, s.begin():
            await s.execute(text("SELECT id FROM users WHERE id = :i FOR UPDATE"), {"i": dev_id})
            await asyncio.sleep(0.3)
            await users_svc.change_role(s, user_id=dev_id, new_role="support")

    async def take():
        async with db.session_factory() as s, s.begin():
            ticket = await tickets_svc.load_ticket(s, ticket_id)
            await asyncio.sleep(0.1)  # смена роли успевает захватить строку
            await tickets_svc.take_ticket(
                s, settings, dev_id, ticket, expected_version=ticket.version
            )

    results = await asyncio.gather(role_change(), take(), return_exceptions=True)
    assert results[0] is None
    assert isinstance(results[1], PermissionDenied)
