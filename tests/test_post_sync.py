import asyncio
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, text

import app.bot.posts as posts
from app.db.models import Priority, Ticket, TicketEvent, User
from app.db.session import SessionLocal, engine
from app.services.tickets import create_ticket, transition


class FakeBot:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.sent = []
        self.on_send = None
        self.started = None

    async def send_message(self, **kwargs):
        if self.fail:
            raise RuntimeError("simulated Telegram failure")
        self.sent.append(kwargs)
        if self.on_send:
            await self.on_send()
        if self.started:
            self.started.set()
            await asyncio.Event().wait()
        return SimpleNamespace(message_id=9191)

    async def edit_message_text(self, **kwargs):
        self.sent.append(kwargs)


async def create_committed_ticket() -> tuple[int, int]:
    async with SessionLocal.begin() as session:
        telegram_id = int(time.time() * 1000000)
        user = User(telegram_id=telegram_id, full_name="Sync test", role="support")
        session.add(user)
        await session.flush()
        ticket = await create_ticket(
            session,
            author=user,
            description="Sync me",
            priority=Priority.MEDIUM,
            request_id=f"REQ-{telegram_id}",
            tenant_id=None,
        )
        return ticket.id, user.id


async def remove_ticket(ticket_id: int, user_id: int) -> None:
    async with SessionLocal.begin() as session:
        await session.execute(delete(TicketEvent).where(TicketEvent.ticket_id == ticket_id))
        await session.execute(delete(Ticket).where(Ticket.id == ticket_id))
        await session.execute(delete(User).where(User.id == user_id))


@pytest.mark.asyncio
async def test_successful_post_sync_persists_message_and_version(monkeypatch):
    ticket_id, user_id = await create_committed_ticket()
    monkeypatch.setattr(posts, "get_settings", lambda: SimpleNamespace(developer_chat_id=-100))
    bot = FakeBot()
    try:
        assert await posts.sync_ticket(bot, ticket_id)
        await posts.sync_outdated(bot)
        async with SessionLocal() as session:
            ticket = await session.get(Ticket, ticket_id)
            assert ticket.message_id == 9191
            assert ticket.post_synced_version == ticket.version == 1
        assert len(bot.sent) == 1
        assert f"#{ticket_id}" in bot.sent[0]["text"]
    finally:
        await remove_ticket(ticket_id, user_id)


@pytest.mark.asyncio
async def test_sync_error_releases_advisory_lock(monkeypatch):
    ticket_id, user_id = await create_committed_ticket()
    monkeypatch.setattr(posts, "get_settings", lambda: SimpleNamespace(developer_chat_id=-100))
    try:
        with pytest.raises(RuntimeError, match="simulated"):
            await posts.sync_ticket(FakeBot(fail=True), ticket_id)
        async with engine.connect() as connection:
            locked = await connection.scalar(
                text("SELECT pg_try_advisory_lock(:seed, :ticket_id)"),
                {"seed": posts.LOCK_SEED, "ticket_id": ticket_id},
            )
            assert locked
            await connection.scalar(
                text("SELECT pg_advisory_unlock(:seed, :ticket_id)"),
                {"seed": posts.LOCK_SEED, "ticket_id": ticket_id},
            )
    finally:
        await remove_ticket(ticket_id, user_id)


@pytest.mark.asyncio
async def test_sync_cancellation_releases_advisory_lock(monkeypatch):
    ticket_id, user_id = await create_committed_ticket()
    monkeypatch.setattr(posts, "get_settings", lambda: SimpleNamespace(developer_chat_id=-100))
    bot = FakeBot()
    bot.started = asyncio.Event()
    try:
        task = asyncio.create_task(posts.sync_ticket(bot, ticket_id))
        await bot.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with engine.connect() as connection:
            locked = await connection.scalar(
                text("SELECT pg_try_advisory_lock(:seed, :ticket_id)"),
                {"seed": posts.LOCK_SEED, "ticket_id": ticket_id},
            )
            assert locked
            await connection.scalar(
                text("SELECT pg_advisory_unlock(:seed, :ticket_id)"),
                {"seed": posts.LOCK_SEED, "ticket_id": ticket_id},
            )
    finally:
        await remove_ticket(ticket_id, user_id)


@pytest.mark.asyncio
async def test_change_during_post_send_is_synced_again(monkeypatch):
    ticket_id, user_id = await create_committed_ticket()
    monkeypatch.setattr(posts, "get_settings", lambda: SimpleNamespace(developer_chat_id=-100))
    async with SessionLocal.begin() as session:
        actor = User(telegram_id=int(time.time() * 1000000) + 1, role="developer")
        session.add(actor)
        await session.flush()
        actor_id = actor.id
    bot = FakeBot()

    async def take_ticket():
        async with SessionLocal.begin() as session:
            actor = await session.get(User, actor_id)
            ticket = await session.get(Ticket, ticket_id)
            await transition(
                session,
                actor=actor,
                ticket_id=ticket_id,
                expected_version=ticket.version,
                expected_status=ticket.status,
                action="take",
            )

    bot.on_send = take_ticket
    try:
        await posts.sync_ticket(bot, ticket_id)
        async with SessionLocal() as session:
            ticket = await session.get(Ticket, ticket_id)
            assert ticket.version == 2
            assert ticket.post_synced_version == 1
        bot.on_send = None
        await posts.sync_outdated(bot)
        async with SessionLocal() as session:
            ticket = await session.get(Ticket, ticket_id)
            assert ticket.post_synced_version == ticket.version == 2
        assert len(bot.sent) == 2
    finally:
        await remove_ticket(ticket_id, user_id)
        async with SessionLocal.begin() as session:
            await session.execute(delete(User).where(User.id == actor_id))
