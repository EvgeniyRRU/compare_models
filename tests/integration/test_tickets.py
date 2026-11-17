# tests/integration/test_tickets.py
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.services import tickets as tickets_svc
from app.services import users as users_svc
from app.services.errors import PermissionDenied, StaleTicketError


async def _make_author_and_ticket(db, settings, *, priority="high"):
    async with db.session_factory() as s, s.begin():
        sup = await users_svc.upsert_user(s, telegram_id=1, first_name="Sup")
        t = await tickets_svc.create_ticket(
            s, settings, sup.id, description="x", priority=priority, request_id="r1"
        )
        author_id, ticket_id = sup.id, t.id
    return author_id, ticket_id


def _utcnow():
    return datetime.now(UTC)


async def test_create_sets_fields_and_reminder(db, settings):
    author_id, ticket_id = await _make_author_and_ticket(db, settings, priority="critical")
    before, after = _utcnow() - timedelta(seconds=2), _utcnow() + timedelta(seconds=2)
    async with db.session_factory() as s:
        row = (
            await s.execute(
                text(
                    "SELECT status, version, message_id, post_synced_version, "
                    "next_reminder_at, author_id FROM tickets WHERE id = :i"
                ),
                {"i": ticket_id},
            )
        ).one()
        event = (
            await s.execute(
                text(
                    "SELECT event_type, new_status, version FROM ticket_events WHERE ticket_id = :i"
                ),
                {"i": ticket_id},
            )
        ).one()
    assert row[:4] == ("new", 1, None, 0)
    assert before <= row[4].replace(tzinfo=UTC) <= after + timedelta(seconds=600)
    assert row[4].replace(tzinfo=UTC) >= before + timedelta(seconds=590)
    assert row[5] == author_id
    assert event == ("created", "new", 1)


async def test_create_bad_priority(db, settings):
    with pytest.raises(ValueError):
        async with db.session_factory() as s, s.begin():
            await users_svc.upsert_user(s, telegram_id=1)
            await tickets_svc.create_ticket(
                s, settings, 1, description="x", priority="urgent", request_id="r1"
            )


async def test_take_by_developer(db, settings):
    author_id, ticket_id = await _make_author_and_ticket(db, settings)
    async with db.session_factory() as s, s.begin():
        dev = await users_svc.upsert_user(s, telegram_id=2)
        await users_svc.change_role(s, user_id=dev.id, new_role="developer")
        dev_id = dev.id
    async with db.session_factory() as s, s.begin():
        t = await tickets_svc.load_ticket(s, ticket_id)
        await tickets_svc.take_ticket(s, settings, dev_id, t, expected_version=1)
    async with db.session_factory() as s:
        row = (
            await s.execute(
                text(
                    "SELECT status, assignee_id, taken_at, next_reminder_at, "
                    "version FROM tickets WHERE id = :i"
                ),
                {"i": ticket_id},
            )
        ).one()
    assert row[0] == "in_progress"
    assert row[1] == dev_id
    assert row[2] is not None
    assert row[3] is None
    assert row[4] == 2


async def test_take_forbidden_for_support_and_norole(db, settings):
    author_id, ticket_id = await _make_author_and_ticket(db, settings)
    for tg, role in ((3, "support"), (4, None)):
        async with db.session_factory() as s, s.begin():
            u = await users_svc.upsert_user(s, telegram_id=tg)
            if role:
                await users_svc.change_role(s, user_id=u.id, new_role=role)
            uid = u.id
        with pytest.raises(PermissionDenied):
            async with db.session_factory() as s, s.begin():
                t = await tickets_svc.load_ticket(s, ticket_id)
                await tickets_svc.take_ticket(s, settings, uid, t, expected_version=t.version)
    async with db.session_factory() as s:
        status = (
            await s.execute(text("SELECT status FROM tickets WHERE id = :i"), {"i": ticket_id})
        ).scalar_one()
    assert status == "new"


async def test_new_cannot_go_to_final(db, settings):
    _, ticket_id = await _make_author_and_ticket(db, settings)
    # developer со статусом new: права есть, но статус запрещён
    async with db.session_factory() as s, s.begin():
        dev = await users_svc.upsert_user(s, telegram_id=2)
        await users_svc.change_role(s, user_id=dev.id, new_role="developer")
        dev_id = dev.id
    with pytest.raises(StaleTicketError):
        async with db.session_factory() as s, s.begin():
            t = await tickets_svc.load_ticket(s, ticket_id)
            await tickets_svc.apply_transition(
                s, settings, dev_id, t, expected_version=1, action="to_resolved"
            )
