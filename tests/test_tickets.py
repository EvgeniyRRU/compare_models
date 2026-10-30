import asyncio

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError

from app.db.models import Ticket, TicketEvent
from app.domain.state_machine import (
    Action,
    AlreadyTaken,
    InvalidTransition,
    PermissionDenied,
    Priority,
    Role,
    StaleTicket,
)
from app.services.tickets import NewTicket
from app.services.users import RoleChangeBlocked, set_role

DATA = NewTicket("Не работает вход", Priority.HIGH, "REQ-1", "T-1")


async def create(sm, svc, users) -> Ticket:
    async with sm() as s, s.begin():
        return await svc.create(s, users["support"].id, DATA)


async def act(sm, svc, tid, action, actor, version, target=None) -> Ticket:
    async with sm() as s, s.begin():
        return await svc.apply(s, tid, action, actor.id, version, target and target.id)


async def test_create_sets_reminder_and_event(sm, svc, users):
    t = await create(sm, svc, users)
    assert (t.id, t.version, t.status, t.post_synced_version) == (1, 1, "new", 0)
    assert t.next_reminder_at is not None and t.message_id is None
    async with sm() as s:
        events = (await s.execute(select(TicketEvent))).scalars().all()
    assert [(e.event_type, e.version) for e in events] == [("created", 1)]


async def test_only_support_and_admin_create(sm, svc, users):
    for key in ("dev", "nobody"):
        with pytest.raises(PermissionDenied):
            async with sm() as s, s.begin():
                await svc.create(s, users[key].id, DATA)
    async with sm() as s, s.begin():
        await svc.create(s, users["admin"].id, DATA)


async def test_one_transition_one_version_one_event(sm, svc, users):
    t = await create(sm, svc, users)
    t = await act(sm, svc, t.id, Action.TAKE, users["dev"], 1)
    assert (t.status, t.version, t.assignee_id) == ("in_progress", 2, users["dev"].id)
    assert t.taken_at is not None and t.next_reminder_at is None
    async with sm() as s:
        n = (await s.execute(select(func.count()).select_from(TicketEvent))).scalar_one()
    assert n == 2


async def test_admin_acts_as_developer(sm, svc, users):
    t = await create(sm, svc, users)
    t = await act(sm, svc, t.id, Action.TAKE, users["admin"], 1)
    t = await act(sm, svc, t.id, Action.RESOLVE, users["admin"], 2)
    assert t.status == "resolved" and t.closed_at is not None


async def test_stale_button_after_cycle(sm, svc, users):
    t = await create(sm, svc, users)
    t = await act(sm, svc, t.id, Action.TAKE, users["dev"], 1)  # v2
    t = await act(sm, svc, t.id, Action.WAIT, users["dev"], 2)  # v3
    t = await act(sm, svc, t.id, Action.RESUME, users["dev"], 3)  # v4: снова in_progress
    assert t.version == 4
    # старая кнопка «Решено» из v2 — статус тот же, но version устарела
    with pytest.raises(StaleTicket):
        await act(sm, svc, t.id, Action.RESOLVE, users["dev"], 2)
    t = await act(sm, svc, t.id, Action.RESOLVE, users["dev"], 4)
    assert t.status == "resolved"


async def test_take_race_only_one_wins(sm, svc, users):
    t = await create(sm, svc, users)
    results = await asyncio.gather(
        act(sm, svc, t.id, Action.TAKE, users["dev"], 1),
        act(sm, svc, t.id, Action.TAKE, users["dev2"], 1),
        return_exceptions=True,
    )
    assert sum(isinstance(r, Ticket) for r in results) == 1
    assert sum(isinstance(r, AlreadyTaken) for r in results) == 1


async def test_take_old_button_after_release_is_stale_or_taken(sm, svc, users):
    t = await create(sm, svc, users)
    t = await act(sm, svc, t.id, Action.TAKE, users["dev"], 1)
    t = await act(sm, svc, t.id, Action.RELEASE, users["dev"], 2)
    assert (t.status, t.assignee_id, t.taken_at, t.version) == ("new", None, None, 3)
    assert t.next_reminder_at is not None
    with pytest.raises(StaleTicket):
        await act(sm, svc, t.id, Action.TAKE, users["dev"], 1)


async def test_new_to_final_forbidden(sm, svc, users):
    t = await create(sm, svc, users)
    with pytest.raises(InvalidTransition):
        await act(sm, svc, t.id, Action.RESOLVE, users["admin"], 1)


async def test_reopen_and_reassign_admin_only(sm, svc, users):
    t = await create(sm, svc, users)
    t = await act(sm, svc, t.id, Action.TAKE, users["dev"], 1)
    with pytest.raises(PermissionDenied):
        await act(sm, svc, t.id, Action.REASSIGN, users["dev"], 2, users["dev2"])
    with pytest.raises(InvalidTransition):  # support не может быть исполнителем
        await act(sm, svc, t.id, Action.REASSIGN, users["admin"], 2, users["support"])
    old_taken = t.taken_at
    t = await act(sm, svc, t.id, Action.REASSIGN, users["admin"], 2, users["dev2"])
    assert (t.assignee_id, t.status, t.version) == (users["dev2"].id, "in_progress", 3)
    assert t.taken_at >= old_taken
    t = await act(sm, svc, t.id, Action.RESOLVE, users["dev2"], 3)
    with pytest.raises(PermissionDenied):
        await act(sm, svc, t.id, Action.REOPEN, users["dev2"], 4)
    t = await act(sm, svc, t.id, Action.REOPEN, users["admin"], 4)
    assert (t.status, t.assignee_id, t.taken_at, t.closed_at) == ("new", None, None, None)
    assert t.next_reminder_at is not None


async def test_db_check_constraints(sm, svc, users):
    t = await create(sm, svc, users)
    for values in (
        {"assignee_id": users["dev"].id},  # new с исполнителем
        {"status": "in_progress"},  # без исполнителя
        {"status": "resolved"},  # без closed_at
    ):
        with pytest.raises(IntegrityError):
            async with sm() as s, s.begin():
                await s.execute(update(Ticket).where(Ticket.id == t.id).values(**values))


async def test_technical_writes_do_not_bump_version(sm, svc, users):
    t = await create(sm, svc, users)
    async with sm() as s, s.begin():
        await s.execute(
            update(Ticket).where(Ticket.id == t.id).values(message_id=10, post_synced_version=1)
        )
    async with sm() as s:
        fresh = await s.get(Ticket, t.id)
    assert fresh.version == 1


async def test_role_downgrade_blocked_with_active_tickets(sm, svc, users):
    t = await create(sm, svc, users)
    await act(sm, svc, t.id, Action.TAKE, users["dev"], 1)
    for new_role in (Role.SUPPORT, None):
        with pytest.raises(RoleChangeBlocked) as exc:
            async with sm() as s, s.begin():
                await set_role(s, users["admin"].id, users["dev"].id, new_role)
        assert exc.value.ticket_ids == [t.id]
    async with sm() as s, s.begin():  # developer -> admin разрешено
        await set_role(s, users["admin"].id, users["dev"].id, Role.ADMIN)
    await act(sm, svc, t.id, Action.RESOLVE, users["dev"], 2)
    async with sm() as s, s.begin():
        await set_role(s, users["admin"].id, users["dev"].id, Role.SUPPORT)


async def test_role_change_admin_only_and_effective_immediately(sm, svc, users):
    with pytest.raises(PermissionDenied):
        async with sm() as s, s.begin():
            await set_role(s, users["dev"].id, users["nobody"].id, Role.DEVELOPER)
    t = await create(sm, svc, users)
    async with sm() as s, s.begin():
        await set_role(s, users["admin"].id, users["nobody"].id, Role.DEVELOPER)
    t = await act(sm, svc, t.id, Action.TAKE, users["nobody"], 1)
    assert t.assignee_id == users["nobody"].id


async def test_role_change_vs_concurrent_take(sm, svc, users):
    """Смена роли (FOR UPDATE) и взятие (FOR SHARE) сериализуются: не остаётся developer-less исполнителя."""
    for _ in range(5):
        t = await create(sm, svc, users)
        res = await asyncio.gather(
            act(sm, svc, t.id, Action.TAKE, users["dev2"], t.version),
            _downgrade(sm, users),
            return_exceptions=True,
        )
        async with sm() as s:
            u = await s.get(type(users["dev2"]), users["dev2"].id, populate_existing=True)
            active = (
                await s.execute(
                    select(func.count())
                    .select_from(Ticket)
                    .where(Ticket.assignee_id == u.id, Ticket.status == "in_progress")
                )
            ).scalar_one()
        assert not (u.role in (None, "support") and active), res
        async with sm() as s, s.begin():
            await s.execute(
                text("UPDATE tickets SET status='new', assignee_id=NULL, taken_at=NULL")
            )
            await s.execute(update(type(u)).where(type(u).id == u.id).values(role="developer"))


async def _downgrade(sm, users):
    async with sm() as s, s.begin():
        await set_role(s, users["admin"].id, users["dev2"].id, Role.SUPPORT)
