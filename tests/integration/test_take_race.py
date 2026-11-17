# tests/integration/test_take_race.py
import asyncio

import pytest
from sqlalchemy import text

from app import metrics as m
from app.services import tickets as tickets_svc
from app.services.errors import AlreadyTakenError, StaleTicketError


async def _setup(db, settings):
    from app.services import users as users_svc

    async with db.session_factory() as s, s.begin():
        sup = await users_svc.upsert_user(s, telegram_id=1)
        dev = await users_svc.upsert_user(s, telegram_id=2)
        await users_svc.change_role(s, user_id=dev.id, new_role="developer")
        t = await tickets_svc.create_ticket(
            s, settings, sup.id, description="x", priority="high", request_id="r1"
        )
    return dev.id, t.id


async def _take(db, settings, actor_id, ticket_id, version):
    async with db.session_factory() as s, s.begin():
        t = await tickets_svc.load_ticket(s, ticket_id)
        await tickets_svc.take_ticket(s, settings, actor_id, t, expected_version=version)


async def test_take_race_exactly_one_winner(db, settings):
    dev_id, tid = await _setup(db, settings)
    results = await asyncio.gather(
        _take(db, settings, dev_id, tid, 1),
        _take(db, settings, dev_id, tid, 1),
        return_exceptions=True,
    )
    successes = [r for r in results if not isinstance(r, Exception)]
    errors = [r for r in results if isinstance(r, Exception)]
    assert len(successes) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], AlreadyTakenError)
    async with db.session_factory() as s:
        row = (
            await s.execute(text("SELECT status, version FROM tickets WHERE id = :i"), {"i": tid})
        ).one()
    assert row == ("in_progress", 2)


async def test_stale_take_button_after_cycle(db, settings):
    """in_progress → waiting_info → in_progress: старая кнопка take не сработает."""
    dev_id, tid = await _setup(db, settings)
    await _take(db, settings, dev_id, tid, 1)

    async def transition(version, action):
        async with db.session_factory() as s, s.begin():
            t = await tickets_svc.load_ticket(s, tid)
            await tickets_svc.apply_transition(
                s, settings, dev_id, t, expected_version=version, action=action
            )

    await transition(2, "to_waiting")
    await transition(3, "to_in_progress")
    with pytest.raises(AlreadyTakenError):
        await _take(db, settings, dev_id, tid, 1)  # старая кнопка (v1, статус new)
    with pytest.raises(StaleTicketError):
        await transition(2, "to_waiting")  # старая кнопка to_waiting (v2), актуальная v4


async def test_each_transition_bumps_version_once(db, settings):
    dev_id, tid = await _setup(db, settings)
    await _take(db, settings, dev_id, tid, 1)
    async with db.session_factory() as s, s.begin():
        t = await tickets_svc.load_ticket(s, tid)
        await tickets_svc.apply_transition(
            s, settings, dev_id, t, expected_version=2, action="to_resolved"
        )
    async with db.session_factory() as s:
        versions = (
            (
                await s.execute(
                    text("SELECT version FROM ticket_events WHERE ticket_id = :i ORDER BY id"),
                    {"i": tid},
                )
            )
            .scalars()
            .all()
        )
    assert versions == [1, 2, 3]


async def test_take_duration_uses_last_entry_to_new(db, settings):
    """После decline (новый вход в new) время до взятия считается от decline."""
    dev_id, tid = await _setup(db, settings)
    await _take(db, settings, dev_id, tid, 1)
    async with db.session_factory() as s, s.begin():
        t = await tickets_svc.load_ticket(s, tid)
        await tickets_svc.apply_transition(
            s, settings, dev_id, t, expected_version=2, action="decline"
        )
    await _take(db, settings, dev_id, tid, 3)
    sample = None
    for metric in m.ticket_take_duration_seconds.collect():
        for smp in metric.samples:
            if smp.name == "ticket_take_duration_seconds_bucket" and smp.labels.get("le") == "+Inf":
                sample = smp.value
    assert sample is not None and sample >= 0
