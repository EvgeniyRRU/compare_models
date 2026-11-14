"""Tests for post synchronization (real PostgreSQL via the conftest fixtures)."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.background import find_unsynced_ticket_ids, sync_pending
from app.db.enums import Priority, Role
from app.db.models import Ticket, User
from app.db.session import AdvisoryLockTimeout, advisory_lock
from app.services.post_sync import PostUnavailable, sync_ticket
from app.services.ticket_service import create_ticket


async def mk_user(session: AsyncSession, telegram_id: int, role: Role | None = None) -> User:
    user = User(telegram_id=telegram_id, role=role)
    session.add(user)
    await session.flush()
    return user


async def new_ticket(session: AsyncSession, author: User) -> Ticket:
    ticket = await create_ticket(
        session,
        author=author,
        priority=Priority.medium,
        description="a test ticket",
        request_id="REQ-1",
    )
    await session.commit()
    return ticket


async def fresh_ticket(session: AsyncSession, ticket_id: int) -> Ticket:
    """Load a ticket with fresh values, bypassing the identity map."""
    stmt = select(Ticket).where(Ticket.id == ticket_id).execution_options(populate_existing=True)
    return (await session.execute(stmt)).scalars().one()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """A session factory bound to the conftest test engine (``NullPool``)."""
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def fixed_render(
    ticket: Ticket, author: User | None, assignee: User | None
) -> tuple[str, Any | None]:
    """A deterministic renderer returning fixed text (used to isolate sync logic)."""
    return "fixed post text", None


class FakePostClient:
    """Records send/edit calls and hands out incrementing message ids."""

    def __init__(
        self,
        *,
        edit_error: type[Exception] | None = None,
        on_send: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.sent: list[str] = []
        self.sent_ids: list[int] = []
        self.edited: list[tuple[int, str]] = []
        self.edit_error = edit_error
        self.on_send = on_send
        self._next_id = 1000

    async def send_post(self, text: str, reply_markup: Any | None = None) -> int:
        self.sent.append(text)
        if self.on_send is not None:
            await self.on_send(text)
        self._next_id += 1
        self.sent_ids.append(self._next_id)
        return self._next_id

    async def edit_post(self, message_id: int, text: str, reply_markup: Any | None = None) -> int:
        self.edited.append((message_id, text))
        if self.edit_error is not None:
            raise self.edit_error()
        return message_id


async def test_first_sync_sends_new_post_and_persists(
    session: AsyncSession,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    author = await mk_user(session, 1001, Role.support)
    await session.commit()
    ticket = await new_ticket(session, author)

    assert ticket.message_id is None
    assert ticket.post_synced_version == 0
    version_before = ticket.version

    client = FakePostClient()
    await sync_ticket(
        engine=engine,
        session_factory=session_factory,
        ticket_id=ticket.id,
        client=client,
        render=fixed_render,
    )

    assert len(client.sent) == 1
    assert client.edited == []

    fresh = await fresh_ticket(session, ticket.id)
    assert fresh.message_id == client.sent_ids[0]
    assert fresh.post_synced_version == version_before
    assert fresh.version == version_before


async def test_second_sync_edits_without_changing_synced_version(
    session: AsyncSession,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    author = await mk_user(session, 1001, Role.support)
    await session.commit()
    ticket = await new_ticket(session, author)

    client = FakePostClient()
    await sync_ticket(
        engine=engine, session_factory=session_factory, ticket_id=ticket.id, client=client
    )
    await sync_ticket(
        engine=engine, session_factory=session_factory, ticket_id=ticket.id, client=client
    )

    assert len(client.sent) == 1
    assert len(client.edited) == 1

    fresh = await fresh_ticket(session, ticket.id)
    assert fresh.version == 1
    assert fresh.post_synced_version == 1
    assert fresh.message_id == client.sent_ids[0]


async def test_edit_unavailable_resends_new_post(
    session: AsyncSession,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    author = await mk_user(session, 1001, Role.support)
    await session.commit()
    ticket = await new_ticket(session, author)

    first = FakePostClient()
    await sync_ticket(
        engine=engine, session_factory=session_factory, ticket_id=ticket.id, client=first
    )
    assert len(first.sent) == 1

    second = FakePostClient(edit_error=PostUnavailable)
    await sync_ticket(
        engine=engine, session_factory=session_factory, ticket_id=ticket.id, client=second
    )

    assert len(second.edited) == 1
    assert len(second.sent) == 1

    fresh = await fresh_ticket(session, ticket.id)
    assert fresh.message_id == second.sent_ids[0]


async def test_change_during_send_persists_rendered_version(
    session: AsyncSession,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    author = await mk_user(session, 1001, Role.support)
    await session.commit()
    ticket = await new_ticket(session, author)  # version == 1

    async def bump_version(_text: str) -> None:
        # Simulate a concurrent business change landing while the post is being sent.
        async with session_factory() as s:
            await s.execute(
                update(Ticket).where(Ticket.id == ticket.id).values(version=Ticket.version + 1)
            )
            await s.commit()

    client = FakePostClient(on_send=bump_version)
    await sync_ticket(
        engine=engine,
        session_factory=session_factory,
        ticket_id=ticket.id,
        client=client,
        render=fixed_render,
    )

    fresh = await fresh_ticket(session, ticket.id)
    assert fresh.version == 2
    # The persisted synced version is the OLD rendered version (1), so it remains
    # behind the business version and the ticket is still eligible for re-sync.
    assert fresh.post_synced_version == 1
    assert fresh.post_synced_version < fresh.version


async def test_find_unsynced_ticket_ids(
    session: AsyncSession,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    author = await mk_user(session, 1001, Role.support)
    await session.commit()
    ticket = await new_ticket(session, author)

    async with session_factory() as s:
        assert await find_unsynced_ticket_ids(s) == [ticket.id]

    await sync_ticket(
        engine=engine, session_factory=session_factory, ticket_id=ticket.id, client=FakePostClient()
    )

    async with session_factory() as s:
        assert await find_unsynced_ticket_ids(s) == []


async def test_sync_pending_publishes_then_idle(
    session: AsyncSession,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    author = await mk_user(session, 1001, Role.support)
    await session.commit()
    ticket = await new_ticket(session, author)

    client = FakePostClient()
    count = await sync_pending(engine=engine, session_factory=session_factory, client=client)
    assert count == 1
    assert len(client.sent) == 1

    fresh = await fresh_ticket(session, ticket.id)
    assert fresh.message_id is not None
    assert fresh.post_synced_version == fresh.version

    assert await sync_pending(engine=engine, session_factory=session_factory, client=client) == 0
    assert len(client.sent) == 1


async def test_advisory_lock_released_on_error(engine: AsyncEngine) -> None:
    key = 424_242
    with pytest.raises(RuntimeError):
        async with advisory_lock(engine, key, blocking=False) as conn:
            assert conn is not None
            raise RuntimeError("boom")

    # A fresh connection must be able to take the lock: it was released on error.
    async with engine.connect() as conn:
        acquired = (
            await conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
        ).scalar_one()
        assert acquired is True
        await conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})


async def test_advisory_lock_timeout(engine: AsyncEngine) -> None:
    key = 999_999
    async with advisory_lock(engine, key, blocking=True) as holder:
        assert holder is not None
        with pytest.raises(AdvisoryLockTimeout):
            async with advisory_lock(engine, key, blocking=True, timeout=1):
                pass


async def test_advisory_lock_nonblocking_yields_none_when_held(engine: AsyncEngine) -> None:
    key = 555_555
    async with advisory_lock(engine, key, blocking=True) as holder:
        assert holder is not None
        async with advisory_lock(engine, key, blocking=False) as contender:
            assert contender is None


async def test_advisory_lock_released_on_cancel(engine: AsyncEngine) -> None:
    key = 777_777
    acquired = asyncio.Event()

    async def holder() -> None:
        async with advisory_lock(engine, key, blocking=True) as conn:
            assert conn is not None
            acquired.set()
            await asyncio.Event().wait()  # suspend inside the lock until cancelled

    task = asyncio.create_task(holder())
    await asyncio.wait_for(acquired.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # A fresh connection can now take the lock: cancellation released it.
    async with engine.connect() as conn:
        got = (
            await conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
        ).scalar_one()
        assert got is True
        await conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})


async def test_sync_persists_updates_updated_at(
    session: AsyncSession,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    author = await mk_user(session, 1001, Role.support)
    await session.commit()
    ticket = await new_ticket(session, author)

    before = (await fresh_ticket(session, ticket.id)).updated_at

    await sync_ticket(
        engine=engine, session_factory=session_factory, ticket_id=ticket.id, client=FakePostClient()
    )

    after = (await fresh_ticket(session, ticket.id)).updated_at
    assert after > before


async def test_find_unsynced_selects_stale_version(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    author = await mk_user(session, 1001, Role.support)
    await session.commit()
    ticket = await new_ticket(session, author)

    # Give it a post (message_id set) but leave it stale: post_synced_version < version.
    async with session_factory() as s:
        await s.execute(
            update(Ticket)
            .where(Ticket.id == ticket.id)
            .values(message_id=12345, post_synced_version=0)
        )
        await s.commit()

    async with session_factory() as s:
        assert await find_unsynced_ticket_ids(s) == [ticket.id]

    # Bring post_synced_version up to version → no longer unsynced.
    async with session_factory() as s:
        await s.execute(update(Ticket).where(Ticket.id == ticket.id).values(post_synced_version=1))
        await s.commit()

    async with session_factory() as s:
        assert await find_unsynced_ticket_ids(s) == []
