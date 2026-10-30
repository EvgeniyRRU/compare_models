import asyncio

import pytest
from sqlalchemy import text

from app.db.models import Ticket, User
from app.domain.state_machine import Action, Priority
from app.services.post_render import TELEGRAM_LIMIT, render_post
from app.services.post_sync import PostSyncer
from app.services.tickets import NewTicket
from tests.fakes import FakePublisher


@pytest.fixture
def pub():
    return FakePublisher()


@pytest.fixture
def syncer(engine, sm, pub):
    return PostSyncer(engine, sm, pub, chat_id=-1, lock_timeout=3)


async def _ticket(sm, svc, users, desc="d"):
    async with sm() as s, s.begin():
        return await svc.create(s, users["support"].id, NewTicket(desc, Priority.HIGH, "R"))


async def _get(sm, tid) -> Ticket:
    async with sm() as s:
        return await s.get(Ticket, tid, populate_existing=True)


async def _locks(engine) -> int:
    async with engine.connect() as c:
        return (
            await c.execute(text("SELECT count(*) FROM pg_locks WHERE locktype='advisory'"))
        ).scalar_one()


async def test_first_publish_then_not_selected_again(sm, svc, users, syncer, pub):
    t = await _ticket(sm, svc, users)
    assert await syncer.find_unsynced() == [t.id]
    await syncer.sync(t.id)
    row = await _get(sm, t.id)
    assert (row.message_id, row.post_synced_version, row.version) == (101, 1, 1)
    assert await syncer.find_unsynced() == []
    assert len(pub.sent) == 1


async def test_business_change_edits_post(sm, svc, users, syncer, pub):
    t = await _ticket(sm, svc, users)
    await syncer.sync(t.id)
    async with sm() as s, s.begin():
        await svc.apply(s, t.id, Action.TAKE, users["dev"].id, 1)
    assert await syncer.find_unsynced() == [t.id]
    await syncer.sync(t.id)
    assert len(pub.sent) == 1 and len(pub.edits) == 1
    assert "В работе" in pub.edits[0][1].text
    assert (await _get(sm, t.id)).post_synced_version == 2


async def test_change_during_send_triggers_resync(sm, svc, users, syncer, pub):
    t = await _ticket(sm, svc, users)
    pub.gate = asyncio.Event()
    task = asyncio.create_task(syncer.sync(t.id))
    await pub.in_send.wait()
    async with sm() as s, s.begin():  # состояние меняется, пока идёт отправка v1
        await svc.apply(s, t.id, Action.TAKE, users["dev"].id, 1)
    pub.gate.set()
    await task
    row = await _get(sm, t.id)
    assert (row.post_synced_version, row.version) == (1, 2)  # сохранена отрендеренная версия
    assert await syncer.find_unsynced() == [t.id]
    await syncer.sync_pending()
    assert (await _get(sm, t.id)).post_synced_version == 2
    assert "В работе" in pub.edits[-1][1].text  # отправлено актуальное состояние


async def test_concurrent_syncs_serialized_never_stale(sm, svc, users, syncer, pub):
    t = await _ticket(sm, svc, users)
    await syncer.sync(t.id)
    async with sm() as s, s.begin():
        await svc.apply(s, t.id, Action.TAKE, users["dev"].id, 1)
    await asyncio.gather(*[syncer.sync(t.id) for _ in range(5)])
    assert all("В работе" in v.text for _, v in pub.edits)
    assert len(pub.sent) == 1


async def test_deleted_post_is_republished(sm, svc, users, syncer, pub):
    t = await _ticket(sm, svc, users)
    await syncer.sync(t.id)
    pub.gone.add(101)
    async with sm() as s, s.begin():
        await svc.apply(s, t.id, Action.TAKE, users["dev"].id, 1)
    await syncer.sync(t.id)
    assert (await _get(sm, t.id)).message_id == 102


async def test_force_new_replaces_message_id_without_version_change(sm, svc, users, syncer, pub):
    t = await _ticket(sm, svc, users)
    await syncer.sync(t.id)
    await syncer.sync(t.id, force_new=True)
    row = await _get(sm, t.id)
    assert (row.message_id, row.version) == (102, 1)


async def test_background_publishes_ticket_without_post(sm, svc, users, syncer, pub):
    await _ticket(sm, svc, users)
    await _ticket(sm, svc, users)
    assert await syncer.sync_pending() == 2
    assert await syncer.sync_pending() == 0


async def test_lock_released_on_error_and_cancel(engine, sm, svc, users, syncer, pub):
    t = await _ticket(sm, svc, users)
    base = await _locks(engine)
    pub.fail_next = RuntimeError("telegram down")
    with pytest.raises(RuntimeError):
        await syncer.sync(t.id)
    assert await _locks(engine) == base

    pub.gate = asyncio.Event()
    pub.in_send.clear()
    task = asyncio.create_task(syncer.sync(t.id))
    await pub.in_send.wait()
    assert await _locks(engine) == base + 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _locks(engine) == base


async def test_busy_lock_skipped_by_background(engine, sm, svc, users, syncer, pub):
    t = await _ticket(sm, svc, users)
    pub.gate = asyncio.Event()
    task = asyncio.create_task(syncer.sync(t.id))
    await pub.in_send.wait()
    assert await syncer.sync(t.id, blocking=False) is False
    pub.gate.set()
    await task


async def test_render_fits_limit(sm, svc, users):
    nasty = "<&>" * 1000  # экранирование утраивает длину
    t = await _ticket(sm, svc, users, desc=nasty)
    async with sm() as s:
        author = await s.get(User, t.author_id)
        view = render_post(await s.get(Ticket, t.id), author, None)
    assert len(view.text) <= TELEGRAM_LIMIT and "…" in view.text


async def test_callbacks_fit_64_bytes_and_carry_version(sm, svc, users):
    t = await _ticket(sm, svc, users)
    async with sm() as s:
        author = await s.get(User, t.author_id)
        view = render_post(await s.get(Ticket, t.id), author, None)
    assert view.buttons[0][0].callback_data == f"t:tk:{t.id}:1"
    assert all(len(b.callback_data.encode()) <= 64 for row in view.buttons for b in row)
