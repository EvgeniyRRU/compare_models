import asyncio
from datetime import timedelta

from sqlalchemy import select, text

from support_bot.db.engine import SessionFactory
from support_bot.db.models import Ticket
from support_bot.domain.enums import Priority, Role
from support_bot.domain.state_machine import Action
from support_bot.services.post_sync import PostSyncService
from support_bot.services.reminders import ReminderService
from support_bot.services.tickets import TicketService
from tests.conftest import FakePublisher, MakeTicket, MakeUser


async def make_due(sf: SessionFactory, tid: int, ago: str = "1 minute") -> None:
    async with sf.begin() as s:
        await s.execute(
            text(f"UPDATE tickets SET next_reminder_at = now() - interval '{ago}' WHERE id = :id"),
            {"id": tid},
        )


async def row(sf: SessionFactory, tid: int) -> Ticket:
    async with sf() as s:
        return (await s.execute(select(Ticket).where(Ticket.id == tid))).scalar_one()


async def test_schedule_on_enter_and_exit_new(
    sf: SessionFactory, tickets: TicketService, make_user: MakeUser, make_ticket: MakeTicket
) -> None:
    support, dev, admin = (
        await make_user(Role.SUPPORT),
        await make_user(Role.DEVELOPER),
        await make_user(Role.ADMIN),
    )
    tid = await make_ticket(support, Priority.MEDIUM)
    assert (await row(sf, tid)).next_reminder_at is not None
    await tickets.apply_action(dev.id, tid, Action.TAKE, 1)
    assert (await row(sf, tid)).next_reminder_at is None
    await tickets.apply_action(dev.id, tid, Action.RELEASE, 2)
    t = await row(sf, tid)
    assert t.next_reminder_at is not None
    assert timedelta(minutes=119) < t.next_reminder_at - t.updated_at <= timedelta(minutes=121)
    await tickets.apply_action(dev.id, tid, Action.TAKE, 3)
    await tickets.apply_action(dev.id, tid, Action.RESOLVE, 4)
    assert (await row(sf, tid)).next_reminder_at is None
    await tickets.apply_action(admin.id, tid, Action.REOPEN, 5)
    assert (await row(sf, tid)).next_reminder_at is not None


async def test_reminder_sent_as_reply_and_rescheduled_without_version_change(
    sf: SessionFactory,
    reminders: ReminderService,
    post_sync: PostSyncService,
    publisher: FakePublisher,
    make_user: MakeUser,
    make_ticket: MakeTicket,
) -> None:
    support = await make_user(Role.SUPPORT)
    tid = await make_ticket(support, Priority.CRITICAL)
    await post_sync.sync(tid)
    await make_due(sf, tid)
    assert await reminders.run_once() == 1
    t = await row(sf, tid)
    assert publisher.reminders == [(publisher.reminders[0][0], t.message_id)]
    assert f"#{tid}" in publisher.reminders[0][0]
    assert t.version == 1 and t.post_synced_version == 1  # кнопки поста не устарели
    assert await post_sync.pending_ids() == []
    assert t.next_reminder_at is not None
    assert await reminders.run_once() == 0  # следующий срок ещё не наступил


async def test_no_catch_up_series_after_downtime(
    sf: SessionFactory,
    reminders: ReminderService,
    publisher: FakePublisher,
    make_user: MakeUser,
    make_ticket: MakeTicket,
) -> None:
    support = await make_user(Role.SUPPORT)
    tid = await make_ticket(support, Priority.CRITICAL)
    await make_due(sf, tid, ago="5 hours")  # простой: пропущено 30 сроков
    assert await reminders.run_once() == 1
    assert await reminders.run_once() == 0
    async with sf() as s:
        in_future = await s.scalar(
            text(
                "SELECT next_reminder_at > now() + interval '9 minutes' FROM tickets WHERE id=:id"
            ),
            {"id": tid},
        )
    assert in_future
    assert len(publisher.reminders) == 1


async def test_no_post_means_no_reply(
    sf: SessionFactory,
    reminders: ReminderService,
    publisher: FakePublisher,
    make_user: MakeUser,
    make_ticket: MakeTicket,
) -> None:
    support = await make_user(Role.SUPPORT)
    tid = await make_ticket(support)
    await make_due(sf, tid)
    await reminders.run_once()
    assert publisher.reminders[0][1] is None


async def test_send_failure_not_retried(
    sf: SessionFactory,
    reminders: ReminderService,
    publisher: FakePublisher,
    make_user: MakeUser,
    make_ticket: MakeTicket,
) -> None:
    support = await make_user(Role.SUPPORT)
    tid = await make_ticket(support)
    await make_due(sf, tid)
    publisher.fail_reminder = RuntimeError("telegram down")
    assert await reminders.run_once() == 0
    publisher.fail_reminder = None
    assert await reminders.run_once() == 0  # срок уже сдвинут
    assert publisher.reminders == []


async def test_taken_ticket_not_reminded(
    sf: SessionFactory,
    reminders: ReminderService,
    tickets: TicketService,
    publisher: FakePublisher,
    make_user: MakeUser,
    make_ticket: MakeTicket,
) -> None:
    support, dev = await make_user(Role.SUPPORT), await make_user(Role.DEVELOPER)
    tid = await make_ticket(support)
    await make_due(sf, tid)
    await tickets.apply_action(dev.id, tid, Action.TAKE, 1)
    assert await reminders.run_once() == 0


async def test_concurrent_workers_no_duplicates(
    sf: SessionFactory,
    tickets: TicketService,
    publisher: FakePublisher,
    make_user: MakeUser,
    make_ticket: MakeTicket,
) -> None:
    support = await make_user(Role.SUPPORT)
    ids = [await make_ticket(support, Priority.CRITICAL) for _ in range(30)]
    for tid in ids:
        await make_due(sf, tid)
    workers = [ReminderService(sf, tickets, publisher, batch_size=7) for _ in range(6)]

    async def drain(worker: ReminderService) -> None:
        while await worker.run_once():
            pass

    await asyncio.gather(*(drain(w) for w in workers))
    reminded = sorted(int(t.split("#")[1].split()[0]) for t, _ in publisher.reminders)
    assert reminded == sorted(ids)
