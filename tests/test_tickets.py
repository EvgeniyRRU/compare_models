import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError

from support_bot.db.engine import SessionFactory
from support_bot.db.models import Ticket, TicketEvent
from support_bot.domain.enums import Priority, Role, TicketEventType, TicketStatus
from support_bot.domain.errors import (
    AccessDeniedError,
    AlreadyTakenError,
    InvalidAssigneeError,
    InvalidTransitionError,
    PermissionDeniedError,
    StaleVersionError,
)
from support_bot.domain.state_machine import Action
from support_bot.domain.validation import TicketData
from support_bot.services.tickets import TicketService
from tests.conftest import MakeTicket, MakeUser


async def get_ticket(sf: SessionFactory, ticket_id: int) -> Ticket:
    async with sf() as s:
        return (await s.execute(select(Ticket).where(Ticket.id == ticket_id))).scalar_one()


async def events(sf: SessionFactory, ticket_id: int) -> list[TicketEvent]:
    async with sf() as s:
        return list(
            await s.scalars(
                select(TicketEvent)
                .where(TicketEvent.ticket_id == ticket_id)
                .order_by(TicketEvent.id)
            )
        )


async def test_create_sets_defaults(
    sf: SessionFactory, make_user: MakeUser, make_ticket: MakeTicket
) -> None:
    support = await make_user(Role.SUPPORT)
    tid = await make_ticket(support, Priority.CRITICAL)
    t = await get_ticket(sf, tid)
    assert t.status is TicketStatus.NEW and t.version == 1 and t.post_synced_version == 0
    assert t.assignee_id is None and t.taken_at is None and t.closed_at is None
    assert t.next_reminder_at is not None
    delta = t.next_reminder_at - t.created_at
    assert timedelta(minutes=9, seconds=59) < delta <= timedelta(minutes=10, seconds=1)
    (ev,) = await events(sf, tid)
    assert ev.event_type is TicketEventType.CREATED and ev.version_after == 1


async def test_ticket_numbers_are_sequential(make_user: MakeUser, make_ticket: MakeTicket) -> None:
    support = await make_user(Role.SUPPORT)
    a = await make_ticket(support)
    b = await make_ticket(support)
    assert b == a + 1


@pytest.mark.parametrize("role", [Role.DEVELOPER, None])
async def test_create_requires_role(
    tickets: TicketService, make_user: MakeUser, role: Role | None
) -> None:
    user = await make_user(role)
    data = TicketData("x", Priority.LOW, "r", None)
    with pytest.raises(PermissionDeniedError if role else AccessDeniedError):
        await tickets.create(user.id, data)


async def test_full_lifecycle_one_increment_per_transition(
    sf: SessionFactory, tickets: TicketService, make_user: MakeUser, make_ticket: MakeTicket
) -> None:
    support, dev = await make_user(Role.SUPPORT), await make_user(Role.DEVELOPER)
    tid = await make_ticket(support)

    r = await tickets.apply_action(dev.id, tid, Action.TAKE, 1)
    assert r.version == 2
    t = await get_ticket(sf, tid)
    assert t.status is TicketStatus.IN_PROGRESS and t.assignee_id == dev.id
    assert t.taken_at is not None and t.next_reminder_at is None

    r = await tickets.apply_action(dev.id, tid, Action.TO_WAITING, 2)
    r = await tickets.apply_action(dev.id, tid, Action.TO_WORK, 3)
    r = await tickets.apply_action(dev.id, tid, Action.RESOLVE, 4)
    t = await get_ticket(sf, tid)
    assert (t.status, t.version) == (TicketStatus.RESOLVED, 5)
    assert t.closed_at is not None and t.assignee_id == dev.id

    evs = await events(sf, tid)
    assert [e.version_after for e in evs] == [1, 2, 3, 4, 5]
    assert [e.event_type for e in evs] == [
        TicketEventType.CREATED,
        TicketEventType.TAKEN,
        TicketEventType.STATUS_CHANGED,
        TicketEventType.STATUS_CHANGED,
        TicketEventType.STATUS_CHANGED,
    ]
    assert evs[1].old_assignee_id is None and evs[1].new_assignee_id == dev.id


async def test_release_resets_and_reschedules(
    sf: SessionFactory, tickets: TicketService, make_user: MakeUser, make_ticket: MakeTicket
) -> None:
    support, dev = await make_user(Role.SUPPORT), await make_user(Role.DEVELOPER)
    tid = await make_ticket(support)
    await tickets.apply_action(dev.id, tid, Action.TAKE, 1)
    await tickets.apply_action(dev.id, tid, Action.RELEASE, 2)
    t = await get_ticket(sf, tid)
    assert t.status is TicketStatus.NEW and t.assignee_id is None and t.taken_at is None
    assert t.next_reminder_at is not None and t.version == 3
    assert (await events(sf, tid))[-1].event_type is TicketEventType.RELEASED


async def test_admin_acts_as_developer_and_reopens(
    sf: SessionFactory, tickets: TicketService, make_user: MakeUser, make_ticket: MakeTicket
) -> None:
    support, dev, admin = (
        await make_user(Role.SUPPORT),
        await make_user(Role.DEVELOPER),
        await make_user(Role.ADMIN),
    )
    tid = await make_ticket(support)
    await tickets.apply_action(dev.id, tid, Action.TAKE, 1)
    # admin может менять статус чужого взятого обращения
    await tickets.apply_action(admin.id, tid, Action.REJECT, 2)
    # переоткрыть может только admin
    with pytest.raises(PermissionDeniedError):
        await tickets.apply_action(dev.id, tid, Action.REOPEN, 3)
    await tickets.apply_action(admin.id, tid, Action.REOPEN, 3)
    t = await get_ticket(sf, tid)
    assert t.status is TicketStatus.NEW and t.closed_at is None and t.assignee_id is None
    assert t.next_reminder_at is not None
    # admin может брать сам
    await tickets.apply_action(admin.id, tid, Action.TAKE, 4)
    assert (await get_ticket(sf, tid)).assignee_id == admin.id


async def test_other_developer_cannot_change(
    tickets: TicketService, make_user: MakeUser, make_ticket: MakeTicket
) -> None:
    support, dev, dev2 = (
        await make_user(Role.SUPPORT),
        await make_user(Role.DEVELOPER),
        await make_user(Role.DEVELOPER),
    )
    tid = await make_ticket(support)
    await tickets.apply_action(dev.id, tid, Action.TAKE, 1)
    with pytest.raises(PermissionDeniedError):
        await tickets.apply_action(dev2.id, tid, Action.RESOLVE, 2)
    with pytest.raises(PermissionDeniedError):
        await tickets.apply_action(support.id, tid, Action.RELEASE, 2)


async def test_support_cannot_take(
    tickets: TicketService, make_user: MakeUser, make_ticket: MakeTicket
) -> None:
    support = await make_user(Role.SUPPORT)
    tid = await make_ticket(support)
    with pytest.raises(PermissionDeniedError):
        await tickets.apply_action(support.id, tid, Action.TAKE, 1)


async def test_new_to_final_forbidden(
    sf: SessionFactory, tickets: TicketService, make_user: MakeUser, make_ticket: MakeTicket
) -> None:
    support, admin = await make_user(Role.SUPPORT), await make_user(Role.ADMIN)
    tid = await make_ticket(support)
    for action in (Action.RESOLVE, Action.REJECT, Action.DUPLICATE):
        with pytest.raises(InvalidTransitionError):
            await tickets.apply_action(admin.id, tid, action, 1)
    assert (await get_ticket(sf, tid)).version == 1


async def test_concurrent_take_only_one_wins(
    sf: SessionFactory, tickets: TicketService, make_user: MakeUser, make_ticket: MakeTicket
) -> None:
    support = await make_user(Role.SUPPORT)
    devs = [await make_user(Role.DEVELOPER) for _ in range(5)]
    tid = await make_ticket(support)
    results = await asyncio.gather(
        *(tickets.apply_action(d.id, tid, Action.TAKE, 1) for d in devs), return_exceptions=True
    )
    ok = [r for r in results if not isinstance(r, BaseException)]
    errors = [r for r in results if isinstance(r, BaseException)]
    assert len(ok) == 1
    assert all(isinstance(e, AlreadyTakenError) for e in errors), errors
    t = await get_ticket(sf, tid)
    assert t.version == 2
    assert len(await events(sf, tid)) == 2


async def test_take_after_taken_says_already_taken(
    tickets: TicketService, make_user: MakeUser, make_ticket: MakeTicket
) -> None:
    support, d1, d2 = (
        await make_user(Role.SUPPORT),
        await make_user(Role.DEVELOPER),
        await make_user(Role.DEVELOPER),
    )
    tid = await make_ticket(support)
    await tickets.apply_action(d1.id, tid, Action.TAKE, 1)
    with pytest.raises(AlreadyTakenError):
        await tickets.apply_action(d2.id, tid, Action.TAKE, 1)


async def test_old_button_after_cycle_is_stale(
    sf: SessionFactory, tickets: TicketService, make_user: MakeUser, make_ticket: MakeTicket
) -> None:
    support, dev = await make_user(Role.SUPPORT), await make_user(Role.DEVELOPER)
    tid = await make_ticket(support)
    await tickets.apply_action(dev.id, tid, Action.TAKE, 1)  # v2 in_progress
    await tickets.apply_action(dev.id, tid, Action.TO_WAITING, 2)  # v3
    await tickets.apply_action(dev.id, tid, Action.TO_WORK, 3)  # v4 in_progress
    # Кнопка «Решено» из поста версии 2: статус тот же (in_progress), но version другая.
    with pytest.raises(StaleVersionError) as exc_info:
        await tickets.apply_action(dev.id, tid, Action.RESOLVE, 2)
    assert not isinstance(exc_info.value, AlreadyTakenError)
    t = await get_ticket(sf, tid)
    assert (t.status, t.version) == (TicketStatus.IN_PROGRESS, 4)


async def test_stale_take_after_release_cycle(
    tickets: TicketService, make_user: MakeUser, make_ticket: MakeTicket
) -> None:
    support, dev = await make_user(Role.SUPPORT), await make_user(Role.DEVELOPER)
    tid = await make_ticket(support)
    await tickets.apply_action(dev.id, tid, Action.TAKE, 1)
    await tickets.apply_action(dev.id, tid, Action.RELEASE, 2)  # снова new, v3
    with pytest.raises(StaleVersionError) as exc_info:
        await tickets.apply_action(dev.id, tid, Action.TAKE, 1)
    assert not isinstance(exc_info.value, AlreadyTakenError)


async def test_concurrent_conflicting_actions(
    sf: SessionFactory, tickets: TicketService, make_user: MakeUser, make_ticket: MakeTicket
) -> None:
    support, dev, admin = (
        await make_user(Role.SUPPORT),
        await make_user(Role.DEVELOPER),
        await make_user(Role.ADMIN),
    )
    tid = await make_ticket(support)
    await tickets.apply_action(dev.id, tid, Action.TAKE, 1)
    results = await asyncio.gather(
        tickets.apply_action(dev.id, tid, Action.RESOLVE, 2),
        tickets.apply_action(admin.id, tid, Action.REJECT, 2),
        tickets.apply_action(dev.id, tid, Action.RELEASE, 2),
        return_exceptions=True,
    )
    assert sum(not isinstance(r, BaseException) for r in results) == 1
    assert all(isinstance(r, StaleVersionError) for r in results if isinstance(r, BaseException))
    assert (await get_ticket(sf, tid)).version == 3


async def test_reassign(
    sf: SessionFactory, tickets: TicketService, make_user: MakeUser, make_ticket: MakeTicket
) -> None:
    support, d1, d2, admin = (
        await make_user(Role.SUPPORT),
        await make_user(Role.DEVELOPER),
        await make_user(Role.DEVELOPER),
        await make_user(Role.ADMIN),
    )
    tid = await make_ticket(support)
    await tickets.apply_action(d1.id, tid, Action.TAKE, 1)
    before = await get_ticket(sf, tid)
    with pytest.raises(PermissionDeniedError):
        await tickets.reassign(d1.id, tid, 2, d2.id)
    with pytest.raises(InvalidAssigneeError):
        await tickets.reassign(admin.id, tid, 2, support.id)
    r = await tickets.reassign(admin.id, tid, 2, d2.id)
    assert r.version == 3
    t = await get_ticket(sf, tid)
    assert t.assignee_id == d2.id and t.status is TicketStatus.IN_PROGRESS
    assert t.taken_at is not None and before.taken_at is not None and t.taken_at > before.taken_at
    ev = (await events(sf, tid))[-1]
    assert ev.event_type is TicketEventType.REASSIGNED
    assert (ev.old_assignee_id, ev.new_assignee_id) == (d1.id, d2.id)
    with pytest.raises(StaleVersionError):
        await tickets.reassign(admin.id, tid, 2, d1.id)


async def test_reassign_new_ticket_forbidden(
    tickets: TicketService, make_user: MakeUser, make_ticket: MakeTicket
) -> None:
    support, dev, admin = (
        await make_user(Role.SUPPORT),
        await make_user(Role.DEVELOPER),
        await make_user(Role.ADMIN),
    )
    tid = await make_ticket(support)
    with pytest.raises(InvalidTransitionError):
        await tickets.reassign(admin.id, tid, 1, dev.id)


async def test_role_read_from_db_each_time(
    sf: SessionFactory, tickets: TicketService, make_user: MakeUser, make_ticket: MakeTicket
) -> None:
    support, dev = await make_user(Role.SUPPORT), await make_user(Role.DEVELOPER)
    tid = await make_ticket(support)
    async with sf.begin() as s:
        await s.execute(text("UPDATE users SET role = NULL WHERE id = :id"), {"id": dev.id})
    with pytest.raises(AccessDeniedError):
        await tickets.apply_action(dev.id, tid, Action.TAKE, 1)


@pytest.mark.parametrize(
    "values",
    [
        {"status": TicketStatus.IN_PROGRESS},  # без исполнителя
        {"closed_at": text("now()")},  # closed_at у не финального
        {"status": TicketStatus.RESOLVED, "next_reminder_at": None},  # финальный без closed_at
    ],
)
async def test_db_check_constraints(
    sf: SessionFactory, make_user: MakeUser, make_ticket: MakeTicket, values: dict[str, object]
) -> None:
    support = await make_user(Role.SUPPORT)
    tid = await make_ticket(support)
    with pytest.raises(IntegrityError):
        async with sf.begin() as s:
            await s.execute(update(Ticket).where(Ticket.id == tid).values(**values))


async def test_updated_at_trigger(
    sf: SessionFactory, make_user: MakeUser, make_ticket: MakeTicket
) -> None:
    support = await make_user(Role.SUPPORT)
    tid = await make_ticket(support)
    before = (await get_ticket(sf, tid)).updated_at
    await asyncio.sleep(0.01)
    async with sf.begin() as s:
        await s.execute(update(Ticket).where(Ticket.id == tid).values(message_id=5))
    assert (await get_ticket(sf, tid)).updated_at > before
