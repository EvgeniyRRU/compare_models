# tests/integration/test_reminders.py
import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.services import tickets as tickets_svc
from app.services import users as users_svc
from app.services.reminders import ReminderClaim, claim_due_reminders, send_reminders


def _utcnow():
    return datetime.now(UTC)


async def _ticket(db, settings, priority: str = "critical") -> int:
    async with db.session_factory() as s, s.begin():
        u = await users_svc.upsert_user(s, telegram_id=1)
        t = await tickets_svc.create_ticket(
            s, settings, u.id, description="d", priority=priority, request_id="r"
        )
    return t.id


async def _next_reminder(db, tid):
    async with db.session_factory() as s:
        return (
            await s.execute(text("SELECT next_reminder_at FROM tickets WHERE id = :i"), {"i": tid})
        ).scalar_one()


async def test_schedule_on_create(db, settings):
    tid = await _ticket(db, settings)
    nr = await _next_reminder(db, tid)
    now = _utcnow()
    assert now + timedelta(seconds=590) <= nr <= now + timedelta(seconds=610)


async def test_claim_only_due_and_reschedules_from_now(db, settings):
    tid_due = await _ticket(db, settings)
    await _ticket(db, settings)
    async with db.session_factory() as s, s.begin():
        await s.execute(
            text("UPDATE tickets SET next_reminder_at = now() - interval '1 minute' WHERE id = :i"),
            {"i": tid_due},
        )
    claims = await claim_due_reminders(db.session_factory, settings)
    assert [c.ticket_id for c in claims] == [tid_due]
    # нет догоняющих: следующий срок от ТЕКУЩЕГО времени
    nr = await _next_reminder(db, tid_due)
    now = _utcnow()
    assert now < nr <= now + timedelta(seconds=settings.reminder_interval("critical") + 5)


async def test_claim_no_duplicates_concurrent_workers(db, settings):
    for _ in range(5):
        await _ticket(db, settings)
    async with db.session_factory() as s, s.begin():
        await s.execute(text("UPDATE tickets SET next_reminder_at = now() - interval '1 minute'"))
    c1, c2 = await asyncio.gather(
        claim_due_reminders(db.session_factory, settings),
        claim_due_reminders(db.session_factory, settings),
    )
    ids = [c.ticket_id for c in c1] + [c.ticket_id for c in c2]
    assert len(ids) == len(set(ids)) == 5
    c3, c4 = await asyncio.gather(
        claim_due_reminders(db.session_factory, settings),
        claim_due_reminders(db.session_factory, settings),
    )
    assert c3 == [] and c4 == []


async def test_take_clears_reminder_and_stops_claims(db, settings):
    tid = await _ticket(db, settings)
    async with db.session_factory() as s, s.begin():
        await s.execute(
            text("UPDATE tickets SET next_reminder_at = now() - interval '1 minute' WHERE id = :i"),
            {"i": tid},
        )
    claims = await claim_due_reminders(db.session_factory, settings)
    assert len(claims) == 1
    async with db.session_factory() as s, s.begin():
        dev = await users_svc.upsert_user(s, telegram_id=2)
        await users_svc.change_role(s, user_id=dev.id, new_role="developer")
        t = await tickets_svc.load_ticket(s, tid)
        await tickets_svc.take_ticket(s, settings, dev.id, t, expected_version=t.version)
    assert await _next_reminder(db, tid) is None
    assert await claim_due_reminders(db.session_factory, settings) == []


async def test_decline_reschedules(db, settings):
    tid = await _ticket(db, settings)
    async with db.session_factory() as s, s.begin():
        dev = await users_svc.upsert_user(s, telegram_id=2)
        await users_svc.change_role(s, user_id=dev.id, new_role="developer")
        t = await tickets_svc.load_ticket(s, tid)
        await tickets_svc.take_ticket(s, settings, dev.id, t, expected_version=t.version)
        t = await tickets_svc.load_ticket(s, tid)
        await tickets_svc.apply_transition(
            s, settings, dev.id, t, expected_version=t.version, action="decline"
        )
    nr = await _next_reminder(db, tid)
    assert nr is not None


async def test_send_as_reply_to_post(db, settings, bot):
    tid = await _ticket(db, settings)
    async with db.session_factory() as s, s.begin():
        await s.execute(text("UPDATE tickets SET message_id = 777 WHERE id = :i"), {"i": tid})
    await send_reminders(
        settings, bot, [ReminderClaim(ticket_id=tid, priority="critical", message_id=777)]
    )
    assert len(bot.sent) == 1
    assert bot.sent[0]["reply_to_message_id"] == 777
    assert f"#{tid}" in bot.sent[0]["text"]
    assert bot.sent[0]["chat_id"] == settings.dev_chat_id


async def test_send_without_post_has_no_reply(db, settings, bot):
    tid = await _ticket(db, settings)
    await send_reminders(
        settings, bot, [ReminderClaim(ticket_id=tid, priority="low", message_id=None)]
    )
    assert len(bot.sent) == 1
    assert bot.sent[0].get("reply_to_message_id") is None


async def test_send_failure_no_retry(db, settings, bot):
    """Одна попытка на срок: сбой логируется, повторной отправки нет."""
    tid = await _ticket(db, settings)
    bot.fail_next_send = RuntimeError("telegram down")
    await send_reminders(
        settings, bot, [ReminderClaim(ticket_id=tid, priority="high", message_id=None)]
    )
    assert bot.fail_next_send is None  # исключение потреблено, без ретрая
    await send_reminders(
        settings, bot, [ReminderClaim(ticket_id=tid, priority="high", message_id=None)]
    )
    assert len(bot.sent) == 1
