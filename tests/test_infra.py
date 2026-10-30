import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text, update

from app.db.models import ProcessedUpdate, Ticket
from app.domain.state_machine import Action, Priority
from app.services import reminders
from app.services.tickets import NewTicket
from app.services.updates import claim_update, purge_old
from tests.conftest import INTERVALS


async def _new_ticket(sm, svc, users, priority=Priority.CRITICAL):
    async with sm() as s, s.begin():
        return await svc.create(s, users["support"].id, NewTicket("d", priority, "R"))


async def _make_due(sm, tid):
    async with sm() as s, s.begin():
        await s.execute(
            update(Ticket).where(Ticket.id == tid).values(next_reminder_at=func.now() - timedelta(hours=5))
        )


async def test_claim_update_dedup(sm):
    assert await claim_update(sm, 100) is True
    assert await claim_update(sm, 100) is False


async def test_dedup_record_survives_handler_failure(sm):
    assert await claim_update(sm, 7)
    with pytest.raises(RuntimeError):
        raise RuntimeError("business failure")  # обработка не откатывает уже закоммиченную запись
    async with sm() as s:
        assert await s.get(ProcessedUpdate, 7) is not None
    assert await claim_update(sm, 7) is False


async def test_purge_old_updates(sm):
    await claim_update(sm, 1)
    await claim_update(sm, 2)
    async with sm() as s, s.begin():
        await s.execute(
            update(ProcessedUpdate)
            .where(ProcessedUpdate.update_id == 1)
            .values(created_at=func.now() - timedelta(hours=49))
        )
        assert await purge_old(s, timedelta(hours=48)) == 1


async def test_reminder_schedule_and_no_catch_up(sm, svc, users):
    t = await _new_ticket(sm, svc, users)
    async with sm() as s, s.begin():
        assert await reminders.claim_due(s, INTERVALS) == []  # срок в будущем
    await _make_due(sm, t.id)  # «простой» на 5 часов при интервале 10 минут
    async with sm() as s, s.begin():
        due = await reminders.claim_due(s, INTERVALS)
    assert [d.ticket_id for d in due] == [t.id]
    async with sm() as s, s.begin():  # серии догоняющих напоминаний нет
        assert await reminders.claim_due(s, INTERVALS) == []
        row = await s.get(Ticket, t.id)
        assert row.version == 1  # перенос срока version не меняет
        nxt = (await s.execute(select(Ticket.next_reminder_at - func.now()).where(Ticket.id == t.id))).scalar_one()
    assert timedelta(minutes=9) < nxt <= timedelta(minutes=10)


async def test_reminder_cleared_on_take_and_set_on_release(sm, svc, users):
    t = await _new_ticket(sm, svc, users, Priority.LOW)
    async with sm() as s, s.begin():
        t = await svc.apply(s, t.id, Action.TAKE, users["dev"].id, 1)
        assert t.next_reminder_at is None
        t = await svc.apply(s, t.id, Action.RELEASE, users["dev"].id, 2)
        left = (await s.execute(select(Ticket.next_reminder_at - func.now()).where(Ticket.id == t.id))).scalar_one()
    assert timedelta(hours=7) < left <= timedelta(hours=8)


async def test_taken_ticket_not_reminded(sm, svc, users):
    t = await _new_ticket(sm, svc, users)
    async with sm() as s, s.begin():
        await svc.apply(s, t.id, Action.TAKE, users["dev"].id, 1)
    async with sm() as s, s.begin():
        await s.execute(update(Ticket).values(next_reminder_at=func.now() - timedelta(hours=1)))
    async with sm() as s, s.begin():
        assert await reminders.claim_due(s, INTERVALS) == []


async def test_concurrent_workers_no_duplicates(sm, svc, users):
    for _ in range(10):
        t = await _new_ticket(sm, svc, users)
        await _make_due(sm, t.id)

    async def worker():
        async with sm() as s, s.begin():
            due = await reminders.claim_due(s, INTERVALS, limit=4)
            await asyncio.sleep(0.2)  # держим блокировки, пока соседи выбирают
            return [d.ticket_id for d in due]

    results = await asyncio.gather(*[worker() for _ in range(4)])
    flat = [i for r in results for i in r]
    assert len(flat) == len(set(flat)) == 10
