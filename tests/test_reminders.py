from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import update

from support_bot.db.enums import TicketPriority, TicketStatus, UserRole
from support_bot.db.models.ticket import Ticket
from support_bot.domain.state_machine import TicketAction
from support_bot.services import tickets as tickets_service
from support_bot.services.reminders import claim_due_reminders
from support_bot.workers.reminders import reminders_tick


async def _set_due(session_factory, ticket_id: int, *, seconds_ago: int = 60) -> None:
    async with session_factory() as session, session.begin():
        await session.execute(
            update(Ticket)
            .where(Ticket.id == ticket_id)
            .values(next_reminder_at=datetime.now(UTC) - timedelta(seconds=seconds_ago))
        )


async def test_reminder_scheduled_on_create(session_factory, make_user, make_ticket) -> None:
    author = await make_user(telegram_id=300, role=UserRole.SUPPORT)
    ticket = await make_ticket(author_id=author.id, priority=TicketPriority.HIGH)
    assert ticket.next_reminder_at is not None
    delta = ticket.next_reminder_at - datetime.now(UTC)
    assert timedelta(minutes=28) < delta < timedelta(minutes=32)


async def test_exit_from_new_clears_reminder(session_factory, make_user, make_ticket) -> None:
    author = await make_user(telegram_id=301, role=UserRole.SUPPORT)
    developer = await make_user(telegram_id=302, role=UserRole.DEVELOPER)
    ticket = await make_ticket(author_id=author.id)
    async with session_factory() as session:
        taken = await tickets_service.take_ticket(
            session, ticket_id=ticket.id, expected_version=1, actor_id=developer.id
        )
    assert taken.next_reminder_at is None


async def test_decline_reopens_reminder_schedule(
    session_factory, make_user, make_ticket, policy
) -> None:
    author = await make_user(telegram_id=303, role=UserRole.SUPPORT)
    developer = await make_user(telegram_id=304, role=UserRole.DEVELOPER)
    ticket = await make_ticket(author_id=author.id, priority=TicketPriority.LOW)
    async with session_factory() as session:
        await tickets_service.take_ticket(
            session, ticket_id=ticket.id, expected_version=1, actor_id=developer.id
        )
    async with session_factory() as session:
        declined = await tickets_service.change_status(
            session,
            ticket_id=ticket.id,
            expected_version=2,
            action=TicketAction.DECLINE,
            actor_id=developer.id,
            policy=policy,
        )
    assert declined.next_reminder_at is not None
    assert declined.version == 3


async def test_no_catch_up_burst_after_downtime(
    session_factory, make_user, make_ticket, policy
) -> None:
    author = await make_user(telegram_id=305, role=UserRole.SUPPORT)
    ticket = await make_ticket(author_id=author.id, priority=TicketPriority.CRITICAL)
    await _set_due(session_factory, ticket.id, seconds_ago=7200)

    now = datetime.now(UTC)
    reminders = await claim_due_reminders(session_factory, policy, batch_size=10, now=now)
    assert [reminder.ticket_id for reminder in reminders] == [ticket.id]

    async with session_factory() as session:
        current = await session.get(Ticket, ticket.id)
    assert current is not None
    assert current.next_reminder_at is not None
    delta = current.next_reminder_at - now
    assert timedelta(minutes=9) < delta < timedelta(minutes=11)

    # No backlog: the next claim is empty.
    assert await claim_due_reminders(session_factory, policy, batch_size=10) == []


async def test_concurrent_workers_claim_disjoint_sets(
    session_factory, make_user, make_ticket, policy
) -> None:
    author = await make_user(telegram_id=306, role=UserRole.SUPPORT)
    first = await make_ticket(author_id=author.id, request_id="A")
    second = await make_ticket(author_id=author.id, request_id="B")
    await _set_due(session_factory, first.id)
    await _set_due(session_factory, second.id)

    left, right = await asyncio.gather(
        claim_due_reminders(session_factory, policy, batch_size=10),
        claim_due_reminders(session_factory, policy, batch_size=10),
    )
    ids = [reminder.ticket_id for reminder in left + right]
    assert sorted(ids) == sorted([first.id, second.id])
    assert len(ids) == len(set(ids))


async def test_reminder_shift_does_not_bump_version(
    session_factory, make_user, make_ticket, policy
) -> None:
    author = await make_user(telegram_id=307, role=UserRole.SUPPORT)
    developer = await make_user(telegram_id=308, role=UserRole.DEVELOPER)
    ticket = await make_ticket(author_id=author.id)
    await _set_due(session_factory, ticket.id)
    await claim_due_reminders(session_factory, policy, batch_size=10)

    async with session_factory() as session:
        current = await session.get(Ticket, ticket.id)
    assert current is not None
    assert current.version == 1

    # A button rendered before the reminder shift is still valid.
    async with session_factory() as session:
        taken = await tickets_service.take_ticket(
            session, ticket_id=ticket.id, expected_version=1, actor_id=developer.id
        )
    assert taken.status is TicketStatus.IN_PROGRESS
    assert taken.version == 2


async def test_worker_sends_reminder_reply(
    session_factory, publisher, make_user, make_ticket, policy, settings
) -> None:
    author = await make_user(telegram_id=309, role=UserRole.SUPPORT)
    ticket = await make_ticket(author_id=author.id, priority=TicketPriority.CRITICAL)
    # Simulate a published post.
    async with session_factory() as session, session.begin():
        await session.execute(
            update(Ticket)
            .where(Ticket.id == ticket.id)
            .values(message_id=555, next_reminder_at=datetime.now(UTC) - timedelta(minutes=1))
        )

    sent = await reminders_tick(
        session_factory,
        publisher,
        policy,
        chat_id=settings.dev_chat_id,
        batch_size=10,
    )
    assert sent == 1
    chat_id, text, reply_to = publisher.reminders[0]
    assert chat_id == settings.dev_chat_id
    assert f"#{ticket.id}" in text
    assert reply_to == 555


async def test_reminder_failure_is_logged_not_retried(
    session_factory, publisher, make_user, make_ticket, policy, settings
) -> None:
    author = await make_user(telegram_id=310, role=UserRole.SUPPORT)
    ticket = await make_ticket(author_id=author.id)
    await _set_due(session_factory, ticket.id)
    publisher.fail_reminder = True
    sent = await reminders_tick(
        session_factory,
        publisher,
        policy,
        chat_id=settings.dev_chat_id,
        batch_size=10,
    )
    assert sent == 0
    publisher.fail_reminder = False
    # The deadline already moved, so the failed reminder is not retried.
    assert (
        await reminders_tick(
            session_factory,
            publisher,
            policy,
            chat_id=settings.dev_chat_id,
            batch_size=10,
        )
        == 0
    )
