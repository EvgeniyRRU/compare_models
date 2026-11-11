import asyncio
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from support_bot.config import Settings
from support_bot.db.models import Ticket
from support_bot.domain.state_machine import Action, Status
from support_bot.services import reminders as svc
from support_bot.services import tickets as ticket_svc
from tests.helpers import NOW, add_user


class FakeReminderSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, int | None]] = []
        self.fail = False

    async def remind(self, text: str, reply_to_message_id: int | None) -> None:
        if self.fail:
            raise RuntimeError("telegram down")
        self.sent.append((text, reply_to_message_id))


async def _new_ticket(session: AsyncSession, settings: Settings, priority: str = "critical") -> int:
    support = await add_user(session, 2000, "support")
    ticket = await ticket_svc.create_ticket(
        session,
        settings=settings,
        actor_id=support,
        description="x",
        priority=priority,
        request_id="R",
        tenant_id=None,
        now=NOW,
    )
    await session.commit()
    await session.execute(
        text("UPDATE tickets SET message_id = 555 WHERE id = :id"), {"id": ticket.id}
    )
    await session.commit()
    return ticket.id


async def test_due_ticket_is_claimed_and_next_reminder_moves_from_now(
    session: AsyncSession, engine: AsyncEngine, settings: Settings
) -> None:
    ticket_id = await _new_ticket(session, settings)
    later = NOW + timedelta(minutes=settings.reminder_critical_minutes)

    jobs = await svc.claim_due_reminders(engine, settings=settings, now=later)

    assert [j.ticket_id for j in jobs] == [ticket_id]
    assert jobs[0].reply_to_message_id == 555
    ticket = await session.get(Ticket, ticket_id, populate_existing=True)
    assert ticket is not None
    assert ticket.next_reminder_at == later + timedelta(minutes=settings.reminder_critical_minutes)


async def test_ticket_not_yet_due_is_not_claimed(
    session: AsyncSession, engine: AsyncEngine, settings: Settings
) -> None:
    await _new_ticket(session, settings)

    jobs = await svc.claim_due_reminders(engine, settings=settings, now=NOW + timedelta(minutes=1))

    assert jobs == []


async def test_long_downtime_produces_one_reminder_not_a_backlog(
    session: AsyncSession, engine: AsyncEngine, settings: Settings
) -> None:
    await _new_ticket(session, settings)
    after_outage = NOW + timedelta(hours=5)

    first = await svc.claim_due_reminders(engine, settings=settings, now=after_outage)
    second = await svc.claim_due_reminders(engine, settings=settings, now=after_outage)

    assert len(first) == 1
    assert second == []


async def test_taken_ticket_is_no_longer_reminded(
    session: AsyncSession,
    engine: AsyncEngine,
    settings: Settings,
) -> None:
    ticket_id = await _new_ticket(session, settings)
    dev = await add_user(session, 2001, "developer")
    await ticket_svc.transition(
        session,
        settings=settings,
        actor_id=dev,
        ticket_id=ticket_id,
        action=Action.TAKE,
        expected_version=1,
        expected_status=Status.NEW,
        now=NOW,
    )
    await session.commit()

    jobs = await svc.claim_due_reminders(engine, settings=settings, now=NOW + timedelta(hours=1))

    assert jobs == []


async def test_concurrent_workers_never_claim_the_same_ticket_twice(
    session: AsyncSession, engine: AsyncEngine, settings: Settings
) -> None:
    for i in range(6):
        support = await add_user(session, 3000 + i, "support")
        await ticket_svc.create_ticket(
            session,
            settings=settings,
            actor_id=support,
            description="x",
            priority="low",
            request_id="R",
            tenant_id=None,
            now=NOW,
        )
        await session.commit()

    later = NOW + timedelta(hours=10)
    batches = await asyncio.gather(
        *(svc.claim_due_reminders(engine, settings=settings, now=later, limit=6) for _ in range(3))
    )

    claimed = [job.ticket_id for batch in batches for job in batch]
    assert len(claimed) == len(set(claimed)) == 6


async def test_reminder_pass_replies_to_post_and_sends_once(
    session: AsyncSession, engine: AsyncEngine, settings: Settings
) -> None:
    ticket_id = await _new_ticket(session, settings)
    sender = FakeReminderSender()

    count = await svc.run_reminder_pass(
        engine,
        settings=settings,
        sender=sender,
        now=NOW + timedelta(minutes=settings.reminder_critical_minutes),
    )

    assert count == 1
    text_sent, reply_to = sender.sent[0]
    assert reply_to == 555
    assert f"#{ticket_id}" in text_sent


async def test_failed_send_is_not_retried(
    session: AsyncSession, engine: AsyncEngine, settings: Settings
) -> None:
    await _new_ticket(session, settings)
    sender = FakeReminderSender()
    sender.fail = True
    later = NOW + timedelta(minutes=settings.reminder_critical_minutes)

    await svc.run_reminder_pass(engine, settings=settings, sender=sender, now=later)
    sender.fail = False
    count = await svc.run_reminder_pass(engine, settings=settings, sender=sender, now=later)

    assert count == 0
    assert sender.sent == []


async def test_reminder_without_post_is_sent_without_reply(
    session: AsyncSession,
    engine: AsyncEngine,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ticket_id = await _new_ticket(session, settings)
    async with session_factory.begin() as s:
        await s.execute(
            text("UPDATE tickets SET message_id = NULL WHERE id = :id"), {"id": ticket_id}
        )
    sender = FakeReminderSender()

    await svc.run_reminder_pass(
        engine,
        settings=settings,
        sender=sender,
        now=NOW + timedelta(minutes=settings.reminder_critical_minutes),
    )

    assert sender.sent[0][1] is None
