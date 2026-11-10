import asyncio
from types import SimpleNamespace

import pytest

from app.db.models import Ticket, User
from app.services.posts import render_post, sync_pending, sync_post
from app.services.tickets import create_ticket, transition_ticket


class Bot:
    def __init__(self):
        self.messages = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)
        self.entered.set()
        await self.release.wait()
        return SimpleNamespace(message_id=10)

    async def edit_message_text(self, **kwargs):
        self.messages.append(kwargs)


async def test_sync_records_rendered_version_and_pending_resync(sessions):
    async with sessions.begin() as session:
        support, dev = (
            User(telegram_id=1, name="Support", role="support"),
            User(telegram_id=2, name="Dev", role="developer"),
        )
        session.add_all([support, dev])
        await session.flush()
        ticket = await create_ticket(session, support.id, "x" * 3000, "high", "a", None)
        tid, did = ticket.id, dev.id
        text, _ = render_post(ticket, "a" * 255, None)
        assert len(text) <= 4096
    bot = Bot()
    bot.release.clear()
    engine = sessions.kw["bind"]
    task = asyncio.create_task(sync_post(engine, bot, 1, tid))
    await bot.entered.wait()
    async with sessions.begin() as session:
        await transition_ticket(session, tid, did, "new", 1, "in_progress")
    bot.release.set()
    await task
    async with sessions() as session:
        ticket = await session.get(Ticket, tid)
        assert (ticket.version, ticket.post_synced_version) == (2, 1)
    await sync_pending(engine, bot, 1)
    assert len(bot.messages) == 2
    await sync_pending(engine, bot, 1)
    assert len(bot.messages) == 2


async def test_cancelled_sync_releases_advisory_lock(sessions):
    async with sessions.begin() as session:
        user = User(telegram_id=1, name="Support", role="support")
        session.add(user)
        await session.flush()
        ticket = await create_ticket(session, user.id, "Issue", "high", "a", None)
        tid = ticket.id
    engine = sessions.kw["bind"]
    bot = Bot()
    bot.release.clear()
    task = asyncio.create_task(sync_post(engine, bot, 1, tid))
    await bot.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    bot.release.set()
    assert await sync_post(engine, bot, 1, tid, try_lock=True)


async def test_failed_send_releases_lock_and_next_sync_publishes(sessions):
    async with sessions.begin() as session:
        user = User(telegram_id=1, name="Support", role="support")
        session.add(user)
        await session.flush()
        ticket = await create_ticket(session, user.id, "Issue", "high", "a", None)
        tid = ticket.id
    engine = sessions.kw["bind"]

    class BrokenBot(Bot):
        async def send_message(self, **kwargs):
            raise RuntimeError("network error")

    with pytest.raises(RuntimeError):
        await sync_post(engine, BrokenBot(), 1, tid)
    bot = Bot()
    await sync_pending(engine, bot, 1)
    assert len(bot.messages) == 1
    async with sessions() as session:
        ticket = await session.get(Ticket, tid)
        assert (ticket.message_id, ticket.post_synced_version, ticket.version) == (10, 1, 1)


async def test_overlapping_syncs_render_latest_state_under_lock(sessions):
    async with sessions.begin() as session:
        user = User(telegram_id=1, name="Admin", role="admin")
        session.add(user)
        await session.flush()
        ticket = await create_ticket(session, user.id, "Issue", "high", "a", None)
        tid, uid = ticket.id, user.id
    engine = sessions.kw["bind"]
    bot = Bot()
    bot.release.clear()
    first = asyncio.create_task(sync_post(engine, bot, 1, tid))
    await bot.entered.wait()
    async with sessions.begin() as session:
        await transition_ticket(session, tid, uid, "new", 1, "in_progress")
    second = asyncio.create_task(sync_post(engine, bot, 1, tid))
    await asyncio.sleep(0.05)
    bot.release.set()
    await asyncio.gather(first, second)
    assert "В работе" in bot.messages[-1]["text"]
    async with sessions() as session:
        ticket = await session.get(Ticket, tid)
        assert (ticket.version, ticket.post_synced_version) == (2, 2)
