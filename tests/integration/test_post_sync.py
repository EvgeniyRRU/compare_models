# tests/integration/test_post_sync.py
import asyncio
import time
from datetime import timedelta

import pytest
import telegram
from sqlalchemy import text

from app.services import tickets as tickets_svc
from app.services import users as users_svc
from app.services.post_sync import sync_ticket_post


async def _make_ticket(db, settings) -> int:
    async with db.session_factory() as s, s.begin():
        sup = await users_svc.upsert_user(s, telegram_id=1, first_name="Sup", username="sup")
        t = await tickets_svc.create_ticket(
            s, settings, sup.id, description="desc", priority="high", request_id="R-1"
        )
    return t.id


async def _row(db, tid):
    async with db.session_factory() as s:
        return (
            await s.execute(
                text("SELECT message_id, post_synced_version, version FROM tickets WHERE id = :i"),
                {"i": tid},
            )
        ).one()


async def _dev_id(db) -> int:
    async with db.session_factory() as s, s.begin():
        dev = await users_svc.upsert_user(s, telegram_id=2, first_name="Dev")
        await users_svc.change_role(s, user_id=dev.id, new_role="developer")
        return dev.id


async def _take(db, settings, tid):
    dev_id = await _dev_id(db)
    async with db.session_factory() as s, s.begin():
        t = await tickets_svc.load_ticket(s, tid)
        await tickets_svc.take_ticket(s, settings, dev_id, t, expected_version=t.version)


async def _selector_ids(db):
    from app.workers import post_sync_iteration  # noqa: F401  (импорт для проверки наличия)

    async with db.session_factory() as s:
        return (
            (
                await s.execute(
                    text(
                        "SELECT id FROM tickets "
                        "WHERE message_id IS NULL OR post_synced_version < version"
                    )
                )
            )
            .scalars()
            .all()
        )


async def test_initial_publish(db, settings, bot):
    tid = await _make_ticket(db, settings)
    await sync_ticket_post(settings, db, bot, tid)
    row = await _row(db, tid)
    assert row[0] is not None
    assert row[1] == row[2] == 1  # post_synced_version == version; version не вырос
    assert len(bot.sent) == 1
    assert bot.sent[0]["chat_id"] == settings.dev_chat_id
    assert f"#{tid}" in bot.sent[0]["text"]
    assert tid not in await _selector_ids(db)


async def test_edit_existing_post(db, settings, bot):
    tid = await _make_ticket(db, settings)
    await sync_ticket_post(settings, db, bot, tid)
    await _take(db, settings, tid)
    await sync_ticket_post(settings, db, bot, tid)
    assert len(bot.sent) == 1
    assert len(bot.edited) == 1
    assert bot.edited[0]["message_id"] == bot.sent[0]["message_id"]
    assert "в работе" in bot.edited[0]["text"]
    row = await _row(db, tid)
    assert row[1] == 2


async def test_not_modified_is_success(db, settings, bot):
    tid = await _make_ticket(db, settings)
    await sync_ticket_post(settings, db, bot, tid)
    await _take(db, settings, tid)
    bot.fail_next_edit = telegram.error.BadRequest("message is not modified")
    await sync_ticket_post(settings, db, bot, tid)
    row = await _row(db, tid)
    assert row[1] == 2


async def test_lost_post_resends(db, settings, bot):
    tid = await _make_ticket(db, settings)
    await sync_ticket_post(settings, db, bot, tid)
    old_msg = bot.sent[0]["message_id"]
    await _take(db, settings, tid)
    bot.fail_next_edit = telegram.error.BadRequest("message to edit not found")
    await sync_ticket_post(settings, db, bot, tid)
    row = await _row(db, tid)
    assert row[0] != old_msg
    assert len(bot.sent) == 2


async def test_force_new_restores_post(db, settings, bot):
    tid = await _make_ticket(db, settings)
    await sync_ticket_post(settings, db, bot, tid)
    old_msg = bot.sent[0]["message_id"]
    await sync_ticket_post(settings, db, bot, tid, force_new=True)
    assert len(bot.sent) == 2
    assert bot.edited == []
    row = await _row(db, tid)
    assert row[0] != old_msg
    assert row[1] == 1  # version не изменилась


async def test_429_retry_uses_retry_after(db, settings, bot, monkeypatch):
    tid = await _make_ticket(db, settings)
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(s):
        sleeps.append(s)
        await real_sleep(0)

    import app.services.post_sync as ps

    monkeypatch.setattr(ps.asyncio, "sleep", fake_sleep)
    bot.fail_next_send = telegram.error.RetryAfter(timedelta(seconds=1))
    await sync_ticket_post(settings, db, bot, tid)
    assert sleeps == [1.0]
    assert len(bot.sent) == 1
    row = await _row(db, tid)
    assert row[1] == 1


async def test_change_during_send_leaves_lag(db, settings, bot):
    """Изменение во время отправки: сохраняется отрендеренная version < текущей."""
    tid = await _make_ticket(db, settings)
    started = asyncio.Event()

    async def hook():
        started.set()
        await _take(db, settings, tid)

    bot.send_hook = hook
    task = asyncio.create_task(sync_ticket_post(settings, db, bot, tid))
    await started.wait()
    await task
    row = await _row(db, tid)
    assert row[1] < row[2]  # отставание
    assert tid in await _selector_ids(db)


async def test_no_stale_post_after_concurrent_changes(db, settings, bot):
    """Фоновые итерации догоняют финальное состояние (без возврата устаревшего статуса)."""
    from app.workers import post_sync_iteration

    tid = await _make_ticket(db, settings)
    await post_sync_iteration(settings, db, bot)
    await _take(db, settings, tid)
    await post_sync_iteration(settings, db, bot)
    dev_id = await _dev_id(db)
    async with db.session_factory() as s, s.begin():
        t = await tickets_svc.load_ticket(s, tid)
        await tickets_svc.apply_transition(
            s, settings, dev_id, t, expected_version=t.version, action="to_resolved"
        )
    await post_sync_iteration(settings, db, bot)
    assert "решено" in bot.edited[-1]["text"]
    row = await _row(db, tid)
    assert row[1] == row[2] == 3


async def test_lock_serializes_concurrent_syncs(db, settings, bot):
    tid = await _make_ticket(db, settings)
    await asyncio.gather(
        sync_ticket_post(settings, db, bot, tid),
        sync_ticket_post(settings, db, bot, tid),
    )
    assert len(bot.sent) == 1
    assert len(bot.edited) == 1
    row = await _row(db, tid)
    assert row[1] == row[2]


async def test_lock_released_on_error(db, settings, bot):
    tid = await _make_ticket(db, settings)
    bot.fail_next_send = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        await sync_ticket_post(settings, db, bot, tid)
    await sync_ticket_post(settings, db, bot, tid)
    assert len(bot.sent) == 1


async def test_lock_released_on_cancel(db, settings, bot):
    tid = await _make_ticket(db, settings)
    started = asyncio.Event()

    async def hook():
        started.set()
        await asyncio.Event().wait()  # вечная блокировка

    bot.send_hook = hook
    task = asyncio.create_task(sync_ticket_post(settings, db, bot, tid))
    await started.wait()
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    bot.send_hook = None
    await sync_ticket_post(settings, db, bot, tid)
    assert len(bot.sent) == 1


async def test_skip_when_lock_busy(db, settings, bot):
    tid = await _make_ticket(db, settings)
    conn = await db.lock_engine.connect()
    try:
        assert (
            await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": tid})
        ).scalar_one()
        t0 = time.monotonic()
        await sync_ticket_post(settings, db, bot, tid, lock_timeout=0.0)
        assert time.monotonic() - t0 < 1.0
        assert bot.sent == []
    finally:
        await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": tid})
        await conn.close()
