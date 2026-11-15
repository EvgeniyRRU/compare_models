"""Post-sync + sweeper tests against real Postgres (advisory locks included)."""

import asyncio

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Priority, Role, Ticket, TicketStatus, User
from app.services import tickets as tickets_svc
from app.services.post_sync import (
    LOCK_NAMESPACE,
    asyncpg_dsn,
    sync_ticket,
    sync_ticket_forced,
)
from app.workers.post_sync_worker import pending_ticket_ids, run_post_sync_worker, sweep_once
from tests.fake_gateway import FakeGateway


@pytest.fixture()
def settings() -> Settings:
    return Settings()


async def mk_ticket(session: AsyncSession, *, n: int = 1) -> Ticket:
    session.add(User(telegram_id=900 + n, username=f"a{n}", full_name="A", role=Role.support))
    session.add(User(telegram_id=910 + n, username=f"d{n}", full_name="D", role=Role.developer))
    await session.flush()
    t = Ticket(
        author_id=900 + n,
        assignee_id=None,
        description="тест",
        priority=Priority.medium,
        request_id=f"REQ-{n}",
        status=TicketStatus.new,
        version=1,
        post_synced_version=0,
    )
    session.add(t)
    await session.commit()
    return t


async def _lock_conn(settings: Settings) -> asyncpg.Connection:
    return await asyncpg.connect(asyncpg_dsn(settings.database_url))


async def test_send_when_no_post(db_session, session_factory, settings) -> None:
    t = await mk_ticket(db_session, n=1)
    gw = FakeGateway()
    assert await sync_ticket(gw, session_factory, settings, t.id) is True
    fresh = await db_session.get(Ticket, t.id)
    await db_session.refresh(fresh)
    assert fresh is not None
    assert gw.kinds() == ["send"]
    assert fresh.message_id == 1000
    assert fresh.post_synced_version == 1
    assert fresh.version == 1
    assert "№" in gw.texts[1000]


async def test_edit_when_post_exists(db_session, session_factory, settings) -> None:
    t = await mk_ticket(db_session, n=2)
    gw = FakeGateway()
    await sync_ticket(gw, session_factory, settings, t.id)
    await sync_ticket(gw, session_factory, settings, t.id)
    assert gw.kinds() == ["send", "edit"]
    fresh = await db_session.get(Ticket, t.id)
    await db_session.refresh(fresh)
    assert fresh is not None and fresh.message_id == 1000


async def test_republish_when_post_deleted(db_session, session_factory, settings) -> None:
    t = await mk_ticket(db_session, n=3)
    gw = FakeGateway()
    await sync_ticket(gw, session_factory, settings, t.id)
    gw.live.clear()  # post deleted in Telegram
    await sync_ticket(gw, session_factory, settings, t.id)
    assert gw.kinds() == ["send", "send"]
    fresh = await db_session.get(Ticket, t.id)
    await db_session.refresh(fresh)
    assert fresh is not None and fresh.message_id == 1001


async def test_forced_sync_republishes(db_session, session_factory, settings) -> None:
    t = await mk_ticket(db_session, n=4)
    gw = FakeGateway()
    await sync_ticket(gw, session_factory, settings, t.id)
    ok = await sync_ticket_forced(gw, session_factory, settings, t.id)
    assert ok is True
    assert gw.kinds() == ["send", "send"]
    fresh = await db_session.get(Ticket, t.id)
    await db_session.refresh(fresh)
    assert fresh is not None and fresh.message_id == 1001


async def test_pending_ids_emptied_after_sync(db_session, session_factory, settings) -> None:
    t = await mk_ticket(db_session, n=5)
    assert t.id in await pending_ticket_ids(session_factory)
    gw = FakeGateway()
    await sweep_once(gw, session_factory, settings)
    assert t.id not in await pending_ticket_ids(session_factory)


async def test_sweeper_resyncs_after_take(db_session, session_factory, settings) -> None:
    t = await mk_ticket(db_session, n=6)
    gw = FakeGateway()
    await sync_ticket(gw, session_factory, settings, t.id)
    await tickets_svc.take(db_session, settings, ticket_id=t.id, actor_id=916, expected_version=1)
    await db_session.commit()  # release the FOR UPDATE lock for the sweeper session
    assert await sweep_once(gw, session_factory, settings) == 1
    assert gw.kinds() == ["send", "edit"]
    fresh = await db_session.get(Ticket, t.id)
    await db_session.refresh(fresh)
    assert fresh is not None and fresh.post_synced_version == 2


async def test_try_lock_skips_when_busy(db_session, session_factory, settings) -> None:
    t = await mk_ticket(db_session, n=7)
    lock = await _lock_conn(settings)
    try:
        acquired = await lock.fetchval("SELECT pg_try_advisory_lock($1, $2)", LOCK_NAMESPACE, t.id)
        assert acquired is True
        gw = FakeGateway()
        done = await sync_ticket(gw, session_factory, settings, t.id)
        assert done is False
        assert gw.calls == []
    finally:
        await lock.fetchval("SELECT pg_advisory_unlock($1, $2)", LOCK_NAMESPACE, t.id)
        await lock.close()


async def test_lock_released_after_error(db_session, session_factory, settings) -> None:
    t = await mk_ticket(db_session, n=8)
    gw = FakeGateway()
    gw.script(RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        await sync_ticket(gw, session_factory, settings, t.id)
    probe = await _lock_conn(settings)
    try:
        got = await probe.fetchval("SELECT pg_try_advisory_lock($1, $2)", LOCK_NAMESPACE, t.id)
        assert got is True
        await probe.fetchval("SELECT pg_advisory_unlock($1, $2)", LOCK_NAMESPACE, t.id)
    finally:
        await probe.close()


async def test_sync_missing_ticket_is_noop(db_session, session_factory, settings) -> None:
    gw = FakeGateway()
    assert await sync_ticket(gw, session_factory, settings, 999_999) is False
    assert gw.calls == []


async def test_lock_released_on_cancel(db_session, session_factory, settings) -> None:
    t = await mk_ticket(db_session, n=9)
    gw = FakeGateway()
    gw.hold = asyncio.Event()
    task = asyncio.create_task(sync_ticket(gw, session_factory, settings, t.id))
    await asyncio.sleep(0.2)  # let it reach the blocked gateway call holding the lock
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    probe = await _lock_conn(settings)
    try:
        got = False
        for _ in range(40):
            got = await probe.fetchval("SELECT pg_try_advisory_lock($1, $2)", LOCK_NAMESPACE, t.id)
            if got:
                break
            await asyncio.sleep(0.05)
        assert got is True
        await probe.fetchval("SELECT pg_advisory_unlock($1, $2)", LOCK_NAMESPACE, t.id)
    finally:
        await probe.close()


async def test_worker_runs_and_stops(db_session, session_factory, settings) -> None:
    t = await mk_ticket(db_session, n=10)
    assert t.id in await pending_ticket_ids(session_factory)
    gw = FakeGateway()
    stop = asyncio.Event()
    worker = asyncio.create_task(
        run_post_sync_worker(gw, session_factory, settings, stop, interval_seconds=0.05)
    )
    done = False
    for _ in range(40):
        if t.id not in await pending_ticket_ids(session_factory):
            done = True
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(worker, timeout=2)
    assert done is True
    assert "send" in gw.kinds()
