import asyncio
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from support_bot.config import Settings
from support_bot.db.models import Ticket
from support_bot.domain.state_machine import Action, Status
from support_bot.services import post_sync
from support_bot.services import tickets as ticket_svc
from support_bot.services.post_sync import (
    LockTimeout,
    PostUnavailable,
    RetryAfter,
    find_out_of_sync,
    sync_pending,
    sync_ticket,
)
from tests.helpers import NOW, add_user


class FakeSender:
    def __init__(self) -> None:
        self.next_id = 100
        self.sent: list[tuple[int, str]] = []
        self.edited: list[tuple[int, str]] = []
        self.send_errors: list[Exception] = []
        self.edit_errors: list[Exception] = []
        self.on_edit: Any = None

    async def send(self, text: str, keyboard: Any) -> int:
        if self.send_errors:
            raise self.send_errors.pop(0)
        self.next_id += 1
        self.sent.append((self.next_id, text))
        return self.next_id

    async def edit(self, message_id: int, text: str, keyboard: Any) -> None:
        if self.on_edit is not None:
            await self.on_edit()
        if self.edit_errors:
            raise self.edit_errors.pop(0)
        self.edited.append((message_id, text))


async def _ticket(session: AsyncSession, settings: Settings) -> int:
    support = await add_user(session, 1000, "support")
    ticket = await ticket_svc.create_ticket(
        session,
        settings=settings,
        actor_id=support,
        description="Нет связи",
        priority="critical",
        request_id="REQ-7",
        tenant_id=None,
        now=NOW,
    )
    await session.commit()
    return ticket.id


async def _take(
    factory: async_sessionmaker[AsyncSession], settings: Settings, ticket_id: int
) -> None:
    dev = await _add_dev(factory)
    async with factory() as s:
        await ticket_svc.transition(
            s,
            settings=settings,
            actor_id=dev,
            ticket_id=ticket_id,
            action=Action.TAKE,
            expected_version=1,
            expected_status=Status.NEW,
            now=NOW,
        )
        await s.commit()


async def _add_dev(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as s:
        return await add_user(s, 1001, "developer")


async def _row(session: AsyncSession, ticket_id: int) -> Ticket:
    session.expire_all()
    ticket = await session.get(Ticket, ticket_id, populate_existing=True)
    assert ticket is not None
    return ticket


async def _lock_is_free(engine: AsyncEngine, ticket_id: int) -> bool:
    async with engine.connect() as conn:
        got = await conn.scalar(text("SELECT pg_try_advisory_lock(7001, :id)"), {"id": ticket_id})
        if got:
            await conn.execute(text("SELECT pg_advisory_unlock(7001, :id)"), {"id": ticket_id})
        await conn.commit()
        return bool(got)


async def test_first_sync_publishes_post_and_stores_message_id(
    session: AsyncSession, engine: AsyncEngine, settings: Settings
) -> None:
    ticket_id = await _ticket(session, settings)
    sender = FakeSender()

    await sync_ticket(
        engine, settings=settings, sender=sender, ticket_id=ticket_id, retry_base_seconds=0
    )

    ticket = await _row(session, ticket_id)
    assert ticket.message_id == 101
    assert ticket.post_synced_version == 1
    assert ticket.version == 1
    assert len(sender.sent) == 1
    assert f"#{ticket_id}" in sender.sent[0][1]


async def test_synced_ticket_is_not_selected_again(
    session: AsyncSession, engine: AsyncEngine, settings: Settings
) -> None:
    ticket_id = await _ticket(session, settings)
    sender = FakeSender()
    await sync_ticket(
        engine, settings=settings, sender=sender, ticket_id=ticket_id, retry_base_seconds=0
    )

    assert await find_out_of_sync(session) == []


async def test_ticket_without_post_is_published_by_background_pass(
    session: AsyncSession, engine: AsyncEngine, settings: Settings
) -> None:
    ticket_id = await _ticket(session, settings)
    sender = FakeSender()

    count = await sync_pending(engine, settings=settings, sender=sender)

    assert count == 1
    assert len(sender.sent) == 1
    assert (await _row(session, ticket_id)).message_id is not None


async def test_business_change_edits_post_without_new_message(
    session: AsyncSession,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    ticket_id = await _ticket(session, settings)
    sender = FakeSender()
    await sync_ticket(
        engine, settings=settings, sender=sender, ticket_id=ticket_id, retry_base_seconds=0
    )
    await _take(session_factory, settings, ticket_id)

    assert await find_out_of_sync(session) == [ticket_id]
    await sync_ticket(
        engine, settings=settings, sender=sender, ticket_id=ticket_id, retry_base_seconds=0
    )

    ticket = await _row(session, ticket_id)
    assert len(sender.sent) == 1
    assert sender.edited[-1][0] == 101
    assert "В работе" in sender.edited[-1][1]
    assert ticket.post_synced_version == 2 == ticket.version


async def test_change_during_send_is_synced_again_with_rendered_version(
    session: AsyncSession,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    ticket_id = await _ticket(session, settings)
    sender = FakeSender()
    await sync_ticket(
        engine, settings=settings, sender=sender, ticket_id=ticket_id, retry_base_seconds=0
    )

    async def take_while_sending() -> None:
        await _take(session_factory, settings, ticket_id)

    sender.on_edit = take_while_sending
    await sync_ticket(
        engine, settings=settings, sender=sender, ticket_id=ticket_id, retry_base_seconds=0
    )

    ticket = await _row(session, ticket_id)
    assert ticket.version == 2
    assert ticket.post_synced_version == 1
    assert await find_out_of_sync(session) == [ticket_id]

    sender.on_edit = None
    await sync_ticket(
        engine, settings=settings, sender=sender, ticket_id=ticket_id, retry_base_seconds=0
    )

    assert "В работе" in sender.edited[-1][1]
    assert (await _row(session, ticket_id)).post_synced_version == 2


async def test_advisory_lock_is_released_after_error(
    session: AsyncSession, engine: AsyncEngine, settings: Settings
) -> None:
    ticket_id = await _ticket(session, settings)
    sender = FakeSender()
    sender.send_errors = [ValueError("boom")]

    with pytest.raises(ValueError):
        await sync_ticket(
            engine, settings=settings, sender=sender, ticket_id=ticket_id, retry_base_seconds=0
        )

    assert await _lock_is_free(engine, ticket_id)


async def test_advisory_lock_is_released_on_cancel(
    session: AsyncSession, engine: AsyncEngine, settings: Settings
) -> None:
    ticket_id = await _ticket(session, settings)
    sender = FakeSender()
    started = asyncio.Event()

    async def hang() -> None:
        started.set()
        await asyncio.Event().wait()

    sender.on_edit = hang
    await sync_ticket(
        engine, settings=settings, sender=sender, ticket_id=ticket_id, retry_base_seconds=0
    )
    await _take_via_engine(engine, settings, ticket_id)

    task = asyncio.create_task(
        sync_ticket(
            engine, settings=settings, sender=sender, ticket_id=ticket_id, retry_base_seconds=0
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await _lock_is_free(engine, ticket_id)


async def _take_via_engine(engine: AsyncEngine, settings: Settings, ticket_id: int) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _take(factory, settings, ticket_id)


async def test_busy_ticket_times_out_and_background_pass_skips_it(
    session: AsyncSession, engine: AsyncEngine, settings: Settings
) -> None:
    ticket_id = await _ticket(session, settings)
    holder = await engine.connect()
    await holder.execute(text("SELECT pg_advisory_lock(7001, :id)"), {"id": ticket_id})
    sender = FakeSender()
    try:
        with pytest.raises(LockTimeout):
            await sync_ticket(
                engine,
                settings=settings,
                sender=sender,
                ticket_id=ticket_id,
                lock_timeout_seconds=0.1,
                retry_base_seconds=0,
            )

        count = await sync_pending(engine, settings=settings, sender=sender)
        assert count == 0
        assert sender.sent == []
    finally:
        await holder.execute(text("SELECT pg_advisory_unlock(7001, :id)"), {"id": ticket_id})
        await holder.close()


async def test_two_replicas_produce_one_post(
    session: AsyncSession, engine: AsyncEngine, settings: Settings
) -> None:
    ticket_id = await _ticket(session, settings)
    sender = FakeSender()

    await asyncio.gather(
        *(
            sync_ticket(
                engine,
                settings=settings,
                sender=sender,
                ticket_id=ticket_id,
                lock_timeout_seconds=5,
                retry_base_seconds=0,
            )
            for _ in range(3)
        )
    )

    assert len(sender.sent) == 1
    assert len(sender.edited) == 2


async def test_deleted_post_is_published_again_and_message_id_replaced(
    session: AsyncSession,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    ticket_id = await _ticket(session, settings)
    sender = FakeSender()
    await sync_ticket(
        engine, settings=settings, sender=sender, ticket_id=ticket_id, retry_base_seconds=0
    )
    await _take(session_factory, settings, ticket_id)
    sender.edit_errors = [PostUnavailable("message to edit not found")]

    await sync_ticket(
        engine, settings=settings, sender=sender, ticket_id=ticket_id, retry_base_seconds=0
    )

    ticket = await _row(session, ticket_id)
    assert len(sender.sent) == 2
    assert ticket.message_id == 102
    assert ticket.post_synced_version == 2


async def test_rate_limit_is_retried_after_retry_after(
    session: AsyncSession, engine: AsyncEngine, settings: Settings
) -> None:
    ticket_id = await _ticket(session, settings)
    sender = FakeSender()
    sender.send_errors = [RetryAfter(0)]

    await sync_ticket(
        engine, settings=settings, sender=sender, ticket_id=ticket_id, retry_base_seconds=0
    )

    assert len(sender.sent) == 1


def test_post_sync_module_exposes_lock_namespace() -> None:
    assert post_sync.LOCK_NAMESPACE == 7001
