import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from support_bot.db.models import TicketEvent
from support_bot.domain.errors import BusinessError, Conflict, Forbidden
from support_bot.services.tickets import TicketService
from support_bot.services.users import UserService

pytestmark = pytest.mark.integration


async def actors(db, settings):
    settings.bootstrap_admin_ids = [1]
    users = UserService(db.sessions, settings)
    admin = await users.touch(1, "Admin", bootstrap=True)
    support = await users.touch(2, "Support")
    developer = await users.touch(3, "Developer")
    other = await users.touch(4, "Other")
    await users.set_role(admin.id, support.id, "support")
    await users.set_role(admin.id, developer.id, "developer")
    await users.set_role(admin.id, other.id, "developer")
    return users, admin, support, developer, other


async def test_ticket_lifecycle_has_exact_version_and_audit(db, settings):
    users, admin, support, dev, other = await actors(db, settings)
    service = TicketService(db.sessions, settings)
    t = await service.create(support.id, "Description", "critical", "REQ", "tenant")
    assert t.status == "new" and t.version == 1 and t.next_reminder_at is not None
    assert t.next_reminder_at - t.created_at >= timedelta(seconds=599)
    for action, actor, dest, status in [
        ("take", dev, None, "in_progress"),
        ("wait", dev, None, "waiting_info"),
        ("reassign", admin, other.id, "waiting_info"),
        ("work", other, None, "in_progress"),
        ("resolve", other, None, "resolved"),
        ("reopen", admin, None, "new"),
        ("take", dev, None, "in_progress"),
        ("refuse", dev, None, "new"),
    ]:
        version = t.version
        t = await service.change(actor.id, t.id, t.version, t.status, action, dest)
        assert t.version == version + 1 and t.status == status
        assert (t.next_reminder_at is not None) == (status == "new")
        assert (t.closed_at is not None) == (status == "resolved")
        if status == "new":
            assert t.assignee_id is None and t.taken_at is None
        else:
            assert t.assignee_id is not None and t.taken_at is not None
    async with db.sessions() as session:
        events = list(
            (await session.scalars(select(TicketEvent).order_by(TicketEvent.version))).all()
        )
    assert [e.version for e in events] == list(range(1, 10))
    assert events[0].event_type == "created" and events[-1].event_type == "refused"


async def test_parallel_take_and_stale_cycle_cannot_change_ticket(db, settings):
    _, admin, support, dev, other = await actors(db, settings)
    service = TicketService(db.sessions, settings)
    t = await service.create(support.id, "D", "low", "R")
    results = await asyncio.gather(
        *[service.change(actor.id, t.id, 1, "new", "take") for actor in (dev, other)],
        return_exceptions=True,
    )
    assert sum(isinstance(r, Conflict) for r in results) == 1
    t = await service.get(t.id)
    old_version = t.version
    t = await service.change(admin.id, t.id, t.version, t.status, "wait")
    t = await service.change(admin.id, t.id, t.version, t.status, "work")
    with pytest.raises(Conflict):
        await service.change(admin.id, t.id, old_version, "in_progress", "resolve")
    assert (await service.get(t.id)).version == t.version


@pytest.mark.parametrize(
    ("description", "priority", "request_id", "tenant"),
    [
        (" ", "low", "R", None),
        ("x" * 3001, "low", "R", None),
        ("D", "urgent", "R", None),
        ("D", "low", "", None),
        ("D", "low", "R" * 65, None),
        ("D", "low", "R", ""),
        ("D", "low", "R", "x" * 65),
    ],
)
async def test_create_validates_without_inserting(
    db, settings, description, priority, request_id, tenant
):
    _, _, support, dev, _ = await actors(db, settings)
    service = TicketService(db.sessions, settings)
    with pytest.raises(BusinessError):
        await service.create(support.id, description, priority, request_id, tenant)
    with pytest.raises(Forbidden):
        await service.create(dev.id, "D", "low", "R")


async def test_demotion_blocked_until_active_tickets_reassigned(db, settings):
    users, admin, support, dev, other = await actors(db, settings)
    service = TicketService(db.sessions, settings)
    t = await service.create(support.id, "D", "medium", "R")
    t = await service.change(dev.id, t.id, 1, "new", "take")
    with pytest.raises(BusinessError) as error:
        await users.set_role(admin.id, dev.id, "support")
    assert error.value.ticket_ids == [t.id]
    with pytest.raises(Forbidden):
        await service.change(admin.id, t.id, t.version, t.status, "reassign", support.id)
    t = await service.change(admin.id, t.id, t.version, t.status, "reassign", other.id)
    assert (await users.set_role(admin.id, dev.id, None)).role is None
    with pytest.raises(Forbidden):
        await service.change(dev.id, t.id, t.version, t.status, "resolve")


@pytest.mark.parametrize("action", ["take", "reassign"])
async def test_assignment_and_role_demotion_serialize(db, settings, action):
    users, admin, support, developer, other = await actors(db, settings)
    service = TicketService(db.sessions, settings)
    t = await service.create(support.id, "D", "low", "R")
    if action == "reassign":
        await users.set_role(admin.id, other.id, "admin")
        t = await service.change(admin.id, t.id, 1, "new", "take")
        admin = other
    results = await asyncio.gather(
        service.change(
            developer.id if action == "take" else admin.id,
            t.id,
            t.version,
            t.status,
            action,
            developer.id if action == "reassign" else None,
        ),
        users.set_role(admin.id, developer.id, "support"),
        return_exceptions=True,
    )
    assert all(not isinstance(r, Exception) or isinstance(r, BusinessError) for r in results)
    saved = await service.get(t.id)
    role = (await users.get(developer.id)).role
    assert not (saved.assignee_id == developer.id and role == "support")
    assert any(isinstance(r, BusinessError) for r in results)


@pytest.mark.parametrize("action", ["reject", "duplicate"])
async def test_final_states_retain_assignment(db, settings, action):
    _, _, support, dev, _ = await actors(db, settings)
    service = TicketService(db.sessions, settings)
    t = await service.create(support.id, "D", "low", "R")
    t = await service.change(dev.id, t.id, 1, "new", "take")
    taken = t.taken_at
    t = await service.change(dev.id, t.id, t.version, t.status, action)
    assert t.assignee_id == dev.id and t.taken_at == taken and t.closed_at is not None


async def test_metrics_only_observe_committed_creation_and_last_new_entry(db, settings):
    from support_bot.observability.metrics import metrics

    _, admin, support, dev, _ = await actors(db, settings)
    service = TicketService(db.sessions, settings)
    counter = metrics.tickets_created.labels("low")
    before = counter._value.get()
    with pytest.raises(RuntimeError):
        async with db.sessions.begin() as session:
            await service.create_in(session, support.id, "Rollback", "low", "R")
            raise RuntimeError("rollback")
    assert counter._value.get() == before
    t = await service.create(support.id, "D", "low", "R")
    assert counter._value.get() == before + 1
    histogram_before = metrics.time_to_take._sum.get()
    t = await service.change(dev.id, t.id, t.version, t.status, "take")
    assert metrics.time_to_take._sum.get() > histogram_before


async def test_current_permissions_are_checked_even_for_stale_versions(db, settings):
    users, admin, support, developer, other = await actors(db, settings)
    service = TicketService(db.sessions, settings)
    ticket = await service.create(support.id, "D", "low", "R")
    ticket = await service.change(developer.id, ticket.id, 1, "new", "take")
    with pytest.raises(Forbidden):
        await service.change(support.id, ticket.id, 1, "new", "take")
    with pytest.raises(Forbidden):
        await service.change(other.id, ticket.id, 1, "in_progress", "resolve")
    with pytest.raises(Forbidden):
        await service.change(developer.id, ticket.id, 1, "resolved", "reopen")
