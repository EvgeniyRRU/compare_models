# tests/integration/test_transitions.py
import pytest
from sqlalchemy import text

from app.services import tickets as tickets_svc
from app.services import users as users_svc
from app.services.errors import PermissionDenied, StaleTicketError


async def _setup(db, settings, *, admin_tg=9):
    async with db.session_factory() as s, s.begin():
        sup = await users_svc.upsert_user(s, telegram_id=1)
        dev = await users_svc.upsert_user(s, telegram_id=2)
        other = await users_svc.upsert_user(s, telegram_id=3)
        admin = await users_svc.upsert_user(s, telegram_id=admin_tg)
        await users_svc.change_role(s, user_id=dev.id, new_role="developer")
        await users_svc.change_role(s, user_id=other.id, new_role="developer")
        await users_svc.change_role(s, user_id=admin.id, new_role="admin")
        t = await tickets_svc.create_ticket(
            s, settings, sup.id, description="x", priority="high", request_id="r1"
        )
        ids = {"sup": sup.id, "dev": dev.id, "other": other.id, "admin": admin.id, "ticket": t.id}
    return ids


async def _transition(db, settings, actor_id, ticket_id, version, action, target_user_id=None):
    async with db.session_factory() as s, s.begin():
        t = await tickets_svc.load_ticket(s, ticket_id)
        await tickets_svc.apply_transition(
            s,
            settings,
            actor_id,
            t,
            expected_version=version,
            action=action,
            target_user_id=target_user_id,
        )


async def _take(db, settings, actor_id, ticket_id, version):
    async with db.session_factory() as s, s.begin():
        t = await tickets_svc.load_ticket(s, ticket_id)
        await tickets_svc.take_ticket(s, settings, actor_id, t, expected_version=version)


async def _row(db, ticket_id):
    async with db.session_factory() as s:
        return (
            await s.execute(
                text(
                    "SELECT status, assignee_id, taken_at, closed_at, next_reminder_at, version "
                    "FROM tickets WHERE id = :i"
                ),
                {"i": ticket_id},
            )
        ).one()


async def test_full_cycle_by_assignee(db, settings):
    ids = await _setup(db, settings)
    tid = ids["ticket"]
    await _take(db, settings, ids["dev"], tid, 1)
    await _transition(db, settings, ids["dev"], tid, 2, "to_waiting")
    assert (await _row(db, tid))[0] == "waiting_info"
    await _transition(db, settings, ids["dev"], tid, 3, "to_in_progress")
    await _transition(db, settings, ids["dev"], tid, 4, "to_resolved")
    row = await _row(db, tid)
    assert row[0] == "resolved"
    assert row[3] is not None  # closed_at
    assert row[5] == 5
    async with db.session_factory() as s:
        events = (
            await s.execute(
                text(
                    "SELECT event_type, version FROM ticket_events WHERE ticket_id=:i ORDER BY id"
                ),
                {"i": tid},
            )
        ).all()
    assert [tuple(e) for e in events] == [
        ("created", 1),
        ("taken", 2),
        ("status_changed", 3),
        ("status_changed", 4),
        ("status_changed", 5),
    ]


async def test_other_developer_cannot_touch(db, settings):
    ids = await _setup(db, settings)
    tid = ids["ticket"]
    await _take(db, settings, ids["dev"], tid, 1)
    with pytest.raises(PermissionDenied):
        await _transition(db, settings, ids["other"], tid, 2, "to_resolved")


async def test_admin_can_act_on_anyone_ticket(db, settings):
    ids = await _setup(db, settings)
    tid = ids["ticket"]
    await _take(db, settings, ids["dev"], tid, 1)
    await _transition(db, settings, ids["admin"], tid, 2, "to_resolved")
    assert (await _row(db, tid))[0] == "resolved"


async def test_decline_resets_assignee_and_reminder(db, settings):
    ids = await _setup(db, settings)
    tid = ids["ticket"]
    await _take(db, settings, ids["dev"], tid, 1)
    await _transition(db, settings, ids["dev"], tid, 2, "decline")
    row = await _row(db, tid)
    assert row[0] == "new"
    assert row[1] is None  # assignee сброшен
    assert row[2] is None  # taken_at сброшен
    assert row[4] is not None  # next_reminder_at снова стоит
    assert row[5] == 3


async def test_reopen_admin_only_resets_fields(db, settings):
    ids = await _setup(db, settings)
    tid = ids["ticket"]
    await _take(db, settings, ids["dev"], tid, 1)
    await _transition(db, settings, ids["dev"], tid, 2, "to_rejected")
    with pytest.raises(PermissionDenied):
        await _transition(db, settings, ids["dev"], tid, 3, "reopen")
    await _transition(db, settings, ids["admin"], tid, 3, "reopen")
    row = await _row(db, tid)
    assert (row[0], row[1], row[2], row[3], row[5]) == ("new", None, None, None, 4)
    assert row[4] is not None  # next_reminder_at


async def test_reassign_admin_only_target_executors(db, settings):
    ids = await _setup(db, settings)
    tid = ids["ticket"]
    # с new нельзя
    with pytest.raises(StaleTicketError):
        await _transition(
            db, settings, ids["admin"], tid, 1, "reassign", target_user_id=ids["other"]
        )
    await _take(db, settings, ids["dev"], tid, 1)
    # не admin не может
    with pytest.raises(PermissionDenied):
        await _transition(db, settings, ids["dev"], tid, 2, "reassign", target_user_id=ids["other"])
    before = (await _row(db, tid))[2]
    # на support — нельзя
    async with db.session_factory() as s, s.begin():
        sup2 = await users_svc.upsert_user(s, telegram_id=5)
        await users_svc.change_role(s, user_id=sup2.id, new_role="support")
        sup2_id = sup2.id
    with pytest.raises(PermissionDenied):
        await _transition(db, settings, ids["admin"], tid, 2, "reassign", target_user_id=sup2_id)
    # на другого developer — можно
    await _transition(db, settings, ids["admin"], tid, 2, "reassign", target_user_id=ids["other"])
    row = await _row(db, tid)
    assert row[0] == "in_progress"  # статус не меняется
    assert row[1] == ids["other"]
    assert row[2] > before  # taken_at сброшен
    assert row[5] == 3
    async with db.session_factory() as s:
        ev = (
            await s.execute(
                text(
                    "SELECT event_type, old_assignee_id, new_assignee_id, version "
                    "FROM ticket_events WHERE ticket_id=:i ORDER BY id DESC LIMIT 1"
                ),
                {"i": tid},
            )
        ).one()
    assert ev == ("reassigned", ids["dev"], ids["other"], 3)


async def test_stale_version_rejected(db, settings):
    ids = await _setup(db, settings)
    tid = ids["ticket"]
    await _take(db, settings, ids["dev"], tid, 1)
    with pytest.raises(StaleTicketError):
        await _transition(db, settings, ids["dev"], tid, 1, "to_resolved")  # version уже 2
