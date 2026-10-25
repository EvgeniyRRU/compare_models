import asyncio

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from support_bot.callbacks import parse_ticket_callback
from support_bot.db.advisory_lock import LockNotAcquiredError, advisory_lock
from support_bot.db.engine import SessionFactory
from support_bot.db.models import Ticket
from support_bot.domain.enums import Role, TicketStatus
from support_bot.domain.errors import PermissionDeniedError
from support_bot.domain.state_machine import Action
from support_bot.services.post_sync import PostSyncService, SyncResult
from support_bot.services.tickets import TicketService
from tests.conftest import FakePublisher, MakeTicket, MakeUser


async def lock_is_free(engine: AsyncEngine, key: int) -> bool:
    async with engine.connect() as conn:
        got = (await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})).scalar()
        if got:
            await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
        return bool(got)


async def ticket_row(sf: SessionFactory, tid: int) -> Ticket:
    async with sf() as s:
        return (await s.execute(select(Ticket).where(Ticket.id == tid))).scalar_one()


async def test_first_sync_publishes_and_not_selected_again(
    sf: SessionFactory,
    post_sync: PostSyncService,
    publisher: FakePublisher,
    make_user: MakeUser,
    make_ticket: MakeTicket,
) -> None:
    support = await make_user(Role.SUPPORT)
    tid = await make_ticket(support)
    assert await post_sync.pending_ids() == [tid]
    outcome = await post_sync.sync(tid)
    assert outcome.result is SyncResult.SENT
    t = await ticket_row(sf, tid)
    assert t.message_id == outcome.message_id and t.post_synced_version == 1 and t.version == 1
    assert await post_sync.pending_ids() == []
    assert await post_sync.sync_pending() == 0
    assert len(publisher.sent) == 1


async def test_background_publishes_ticket_without_post(
    sf: SessionFactory,
    post_sync: PostSyncService,
    publisher: FakePublisher,
    make_user: MakeUser,
    make_ticket: MakeTicket,
) -> None:
    support = await make_user(Role.SUPPORT)
    t1, t2 = await make_ticket(support), await make_ticket(support)
    publisher.fail_send = RuntimeError("telegram down")
    assert await post_sync.sync_safely(t1) is None  # сбой после коммита не теряет обращение
    publisher.fail_send = None
    assert await post_sync.sync_pending() == 2
    assert {(await ticket_row(sf, t)).message_id is not None for t in (t1, t2)} == {True}


async def test_sync_does_not_change_version_or_stale_buttons(
    sf: SessionFactory,
    post_sync: PostSyncService,
    publisher: FakePublisher,
    tickets: TicketService,
    make_user: MakeUser,
    make_ticket: MakeTicket,
) -> None:
    support, dev = await make_user(Role.SUPPORT), await make_user(Role.DEVELOPER)
    tid = await make_ticket(support)
    await post_sync.sync(tid)
    await post_sync.sync(tid)
    message_id = (await ticket_row(sf, tid)).message_id
    assert message_id is not None
    _, keyboard = publisher.posts[message_id]
    cb = parse_ticket_callback(keyboard[0][0].callback_data)
    assert (cb.action, cb.version) == (Action.TAKE, 1)
    assert (await ticket_row(sf, tid)).version == 1
    # кнопка из поста по-прежнему актуальна
    await tickets.apply_action(dev.id, tid, cb.action, cb.version)


async def test_edit_after_change_and_republish_if_gone(
    sf: SessionFactory,
    post_sync: PostSyncService,
    publisher: FakePublisher,
    tickets: TicketService,
    make_user: MakeUser,
    make_ticket: MakeTicket,
) -> None:
    support, dev = await make_user(Role.SUPPORT), await make_user(Role.DEVELOPER)
    tid = await make_ticket(support)
    first = await post_sync.sync(tid)
    await tickets.apply_action(dev.id, tid, Action.TAKE, 1)
    assert await post_sync.pending_ids() == [tid]
    edited = await post_sync.sync(tid)
    assert edited.result is SyncResult.EDITED and edited.rendered_version == 2
    assert first.message_id is not None
    assert "В работе" in publisher.posts[first.message_id][0]

    publisher.gone.add(first.message_id)
    await tickets.apply_action(dev.id, tid, Action.TO_WAITING, 2)
    again = await post_sync.sync(tid)
    assert again.result is SyncResult.SENT and again.message_id != first.message_id
    assert (await ticket_row(sf, tid)).message_id == again.message_id


async def test_change_during_send_triggers_resync(
    sf: SessionFactory,
    post_sync: PostSyncService,
    publisher: FakePublisher,
    tickets: TicketService,
    make_user: MakeUser,
    make_ticket: MakeTicket,
) -> None:
    support, dev = await make_user(Role.SUPPORT), await make_user(Role.DEVELOPER)
    tid = await make_ticket(support)

    async def change_concurrently() -> None:
        publisher.before_return = None
        await tickets.apply_action(dev.id, tid, Action.TAKE, 1)

    publisher.before_return = change_concurrently
    outcome = await post_sync.sync(tid)
    assert outcome.rendered_version == 1
    t = await ticket_row(sf, tid)
    # сохранена именно отрендеренная version, поэтому отставание видно
    assert (t.post_synced_version, t.version) == (1, 2)
    assert await post_sync.pending_ids() == [tid]
    assert await post_sync.sync_pending() == 1
    t = await ticket_row(sf, tid)
    assert t.post_synced_version == 2
    assert t.message_id is not None
    assert "В работе" in publisher.posts[t.message_id][0]


async def test_concurrent_syncs_do_not_restore_stale_status(
    sf: SessionFactory,
    post_sync: PostSyncService,
    publisher: FakePublisher,
    tickets: TicketService,
    make_user: MakeUser,
    make_ticket: MakeTicket,
) -> None:
    support, dev = await make_user(Role.SUPPORT), await make_user(Role.DEVELOPER)
    tid = await make_ticket(support)
    await post_sync.sync(tid)
    message_id = (await ticket_row(sf, tid)).message_id
    assert message_id is not None

    release = asyncio.Event()
    entered = asyncio.Event()

    async def slow() -> None:
        publisher.before_return = None
        entered.set()
        await release.wait()

    # Первая синхронизация рендерит версию 2 и «зависает» в Telegram.
    await tickets.apply_action(dev.id, tid, Action.TAKE, 1)
    publisher.before_return = slow
    first = asyncio.create_task(post_sync.sync(tid))
    await entered.wait()
    # Пока она висит, обращение меняется и запускается вторая синхронизация.
    await tickets.apply_action(dev.id, tid, Action.TO_WAITING, 2)
    second = asyncio.create_task(post_sync.sync(tid))
    await asyncio.sleep(0.3)
    assert not second.done()  # ждёт advisory lock
    release.set()
    r1, r2 = await asyncio.gather(first, second)
    assert (r1.rendered_version, r2.rendered_version) == (2, 3)
    assert publisher.edits[-1][0] == message_id
    assert "Ожидает информации" in publisher.posts[message_id][0]
    assert (await ticket_row(sf, tid)).post_synced_version == 3


async def test_lock_released_on_error(
    engine: AsyncEngine,
    post_sync: PostSyncService,
    publisher: FakePublisher,
    make_user: MakeUser,
    make_ticket: MakeTicket,
) -> None:
    support = await make_user(Role.SUPPORT)
    tid = await make_ticket(support)
    publisher.fail_send = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        await post_sync.sync(tid)
    assert await lock_is_free(engine, tid)


async def test_lock_released_on_cancel(
    engine: AsyncEngine,
    post_sync: PostSyncService,
    publisher: FakePublisher,
    make_user: MakeUser,
    make_ticket: MakeTicket,
) -> None:
    support = await make_user(Role.SUPPORT)
    tid = await make_ticket(support)
    entered = asyncio.Event()

    async def hang() -> None:
        entered.set()
        await asyncio.sleep(3600)

    publisher.before_return = hang
    task = asyncio.create_task(post_sync.sync(tid))
    await entered.wait()
    assert not await lock_is_free(engine, tid)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await lock_is_free(engine, tid)


async def test_lock_timeout_and_try_lock(engine: AsyncEngine) -> None:
    async with advisory_lock(engine, 4242):
        with pytest.raises(LockNotAcquiredError):
            async with advisory_lock(engine, 4242, timeout=0.2):
                pass
    assert await lock_is_free(engine, 4242)


async def test_background_skips_locked(
    engine: AsyncEngine,
    post_sync: PostSyncService,
    publisher: FakePublisher,
    make_user: MakeUser,
    make_ticket: MakeTicket,
) -> None:
    support = await make_user(Role.SUPPORT)
    tid = await make_ticket(support)
    async with advisory_lock(engine, tid):
        assert await post_sync.sync_pending() == 0
    assert publisher.sent == []
    assert await post_sync.sync_pending() == 1


async def test_restore_post_admin_only(
    sf: SessionFactory,
    post_sync: PostSyncService,
    publisher: FakePublisher,
    make_user: MakeUser,
    make_ticket: MakeTicket,
) -> None:
    support, admin = await make_user(Role.SUPPORT), await make_user(Role.ADMIN)
    tid = await make_ticket(support)
    await post_sync.sync(tid)
    old = (await ticket_row(sf, tid)).message_id
    with pytest.raises(PermissionDeniedError):
        await post_sync.restore(support.id, tid)
    outcome = await post_sync.restore(admin.id, tid)
    t = await ticket_row(sf, tid)
    assert outcome.result is SyncResult.SENT and t.message_id != old and t.version == 1
    assert t.status is TicketStatus.NEW
