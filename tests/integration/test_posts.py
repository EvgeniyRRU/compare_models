import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text, update
from telegram.error import NetworkError, RetryAfter

from support_bot.db.models import Ticket, User
from support_bot.services.posts import PostService

pytestmark = pytest.mark.integration


async def new_ticket(db):
    async with db.sessions.begin() as session:
        user = User(telegram_id=1, first_name="Автор", role="support")
        session.add(user)
        await session.flush()
        ticket = Ticket(
            author_id=user.id,
            description="Old",
            request_id="R",
            priority="high",
            next_reminder_at=datetime.now(UTC) + timedelta(hours=1),
        )
        session.add(ticket)
        await session.flush()
        return ticket.id


async def test_reconcile_publishes_missing_post_once_and_preserves_version(db, settings):
    ticket_id = await new_ticket(db)
    transport = AsyncMock()
    transport.publish.return_value = 99
    service = PostService(db.engine, db.sessions, settings, transport)
    assert await service.pending() == [ticket_id]
    await service.reconcile()
    assert await service.pending() == []
    assert not await service.sync(ticket_id)
    transport.publish.assert_awaited_once()
    async with db.sessions() as session:
        ticket = await session.get(Ticket, ticket_id)
        assert (ticket.version, ticket.post_synced_version, ticket.message_id) == (1, 1, 99)


async def test_change_during_send_keeps_rendered_version_then_resyncs(db, settings):
    ticket_id = await new_ticket(db)
    seen = []

    async def send(ticket, author, assignee, force=False):
        seen.append((ticket.version, ticket.description))
        if len(seen) == 1:
            async with db.sessions.begin() as session:
                await session.execute(
                    update(Ticket)
                    .where(Ticket.id == ticket_id)
                    .values(description="New", version=2)
                )
        return 99

    transport = AsyncMock()
    transport.publish.side_effect = send
    service = PostService(db.engine, db.sessions, settings, transport)
    await service.sync(ticket_id)
    async with db.sessions() as session:
        assert (await session.get(Ticket, ticket_id)).post_synced_version == 1
    await service.reconcile()
    assert seen == [(1, "Old"), (2, "New")]


async def test_retry_reads_current_state_instead_of_cached_render(db, settings):
    ticket_id = await new_ticket(db)
    seen = []

    async def send(ticket, author, assignee, force=False):
        seen.append(ticket.description)
        if len(seen) == 1:
            async with db.sessions.begin() as session:
                await session.execute(
                    update(Ticket)
                    .where(Ticket.id == ticket_id)
                    .values(description="Fresh", version=2)
                )
            raise RetryAfter(timedelta(seconds=0))
        return 99

    transport = AsyncMock()
    transport.publish.side_effect = send
    await PostService(db.engine, db.sessions, settings, transport).sync(ticket_id)
    assert seen == ["Old", "Fresh"]
    async with db.sessions() as session:
        assert (await session.get(Ticket, ticket_id)).post_synced_version == 2


async def test_competing_sync_and_background_skip_busy_lock(db, settings):
    ticket_id = await new_ticket(db)
    entered, finish = asyncio.Event(), asyncio.Event()

    async def send(*args, **kwargs):
        entered.set()
        await finish.wait()
        return 99

    transport = AsyncMock()
    transport.publish.side_effect = send
    service = PostService(db.engine, db.sessions, settings, transport)
    task = asyncio.create_task(service.sync(ticket_id))
    await entered.wait()
    assert not await service.sync(ticket_id, wait=False)
    waiting = asyncio.create_task(service.sync(ticket_id))
    finish.set()
    assert await task
    assert not await waiting
    transport.publish.assert_awaited_once()


@pytest.mark.parametrize("cancel", [False, True])
async def test_lock_released_after_error_or_cancellation(db, settings, cancel):
    ticket_id = await new_ticket(db)
    entered = asyncio.Event()

    async def send(*args, **kwargs):
        entered.set()
        if cancel:
            await asyncio.Event().wait()
        raise NetworkError("offline")

    transport = AsyncMock()
    transport.publish.side_effect = send
    settings.telegram_retry_attempts = 1
    service = PostService(db.engine, db.sessions, settings, transport)
    task = asyncio.create_task(service.sync(ticket_id))
    await entered.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else NetworkError):
        await task
    async with db.engine.connect() as connection:
        assert await connection.scalar(text("SELECT pg_try_advisory_lock(:id)"), {"id": ticket_id})
        assert await connection.scalar(text("SELECT pg_advisory_unlock(:id)"), {"id": ticket_id})


async def test_force_replaces_post_without_changing_version(db, settings):
    ticket_id = await new_ticket(db)
    transport = AsyncMock()
    transport.publish.side_effect = [10, 20]
    service = PostService(db.engine, db.sessions, settings, transport)
    await service.sync(ticket_id)
    assert await service.sync(ticket_id, force=True)
    assert transport.publish.call_args.kwargs["force"] is True
    async with db.sessions() as session:
        ticket = await session.get(Ticket, ticket_id)
        assert (ticket.message_id, ticket.version) == (20, 1)


async def test_lock_timeout_does_not_release_another_workers_lock(db, settings):
    ticket_id = await new_ticket(db)
    settings.post_lock_timeout_seconds = 0.02
    async with db.engine.connect() as connection:
        assert await connection.scalar(text("SELECT pg_try_advisory_lock(:id)"), {"id": ticket_id})
        await connection.commit()
        service = PostService(db.engine, db.sessions, settings, AsyncMock())
        with pytest.raises(TimeoutError):
            await service.sync(ticket_id)
        assert await connection.scalar(text("SELECT pg_advisory_unlock(:id)"), {"id": ticket_id})


async def test_unlock_failure_invalidates_connection_and_releases_lock(db, settings, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncConnection

    ticket_id = await new_ticket(db)
    transport = AsyncMock()
    transport.publish.return_value = 99
    original = AsyncConnection.execute

    async def execute(connection, statement, *args, **kwargs):
        if str(statement).startswith("SELECT pg_advisory_unlock"):
            raise RuntimeError("unlock connection broken")
        return await original(connection, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncConnection, "execute", execute)
    with pytest.raises(RuntimeError, match="unlock connection broken"):
        await PostService(db.engine, db.sessions, settings, transport).sync(ticket_id)
    monkeypatch.setattr(AsyncConnection, "execute", original)
    async with db.engine.connect() as connection:
        assert await connection.scalar(text("SELECT pg_try_advisory_lock(:id)"), {"id": ticket_id})
        await connection.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": ticket_id})


async def test_no_open_database_transaction_during_network(db, settings):
    ticket_id = await new_ticket(db)

    async def publish(*args, **kwargs):
        async with db.sessions() as session:
            count = await session.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND state = 'idle in transaction'"
                )
            )
            assert count == 0
        return 99

    transport = AsyncMock()
    transport.publish.side_effect = publish
    await PostService(db.engine, db.sessions, settings, transport).sync(ticket_id)


async def test_concurrent_distinct_tickets_need_only_one_connection_each(db, settings, monkeypatch):
    from contextlib import asynccontextmanager

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from support_bot.services import posts

    first_id = await new_ticket(db)
    async with db.sessions.begin() as session:
        first = await session.get(Ticket, first_id)
        others = [
            Ticket(
                author_id=first.author_id,
                description=f"Ticket {index}",
                request_id=f"R{index}",
                priority="high",
                next_reminder_at=datetime.now(UTC) + timedelta(hours=1),
            )
            for index in range(3)
        ]
        session.add_all(others)
        await session.flush()
        ticket_ids = [first_id, *(ticket.id for ticket in others)]

    engine = create_async_engine(
        settings.database_url, pool_size=2, max_overflow=0, pool_timeout=0.2
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    original_lock = posts.ticket_lock
    first_pair = asyncio.Event()
    entered = 0

    @asynccontextmanager
    async def synchronized_lock(*args, **kwargs):
        nonlocal entered
        async with original_lock(*args, **kwargs) as connection:
            entered += 1
            if entered == 2:
                first_pair.set()
            # Ensure the first pair occupies both connections before either reads.
            # Later tickets can run as soon as a connection is returned.
            await first_pair.wait()
            yield connection

    monkeypatch.setattr(posts, "ticket_lock", synchronized_lock)
    transport = AsyncMock()
    transport.publish.return_value = 99
    service = PostService(engine, sessions, settings, transport)
    try:
        async with asyncio.timeout(3):
            assert (
                await asyncio.gather(*(service.sync(ticket_id) for ticket_id in ticket_ids))
                == [True] * 4
            )
    finally:
        await engine.dispose()
