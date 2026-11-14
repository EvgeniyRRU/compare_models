"""Tests for the role service (real PostgreSQL via the conftest fixtures)."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.enums import Priority, Role, TicketStatus
from app.db.models import Ticket, User
from app.services.role_service import RoleChangeBlocked, UserNotFound, assign_role
from app.services.ticket_service import PermissionDenied


async def mk_user(session: AsyncSession, telegram_id: int, role: Role | None = None) -> User:
    user = User(telegram_id=telegram_id, role=role)
    session.add(user)
    await session.flush()
    return user


async def fresh_role(session: AsyncSession, user_id: int) -> Role | None:
    return await session.scalar(select(User.role).where(User.id == user_id))


async def mk_active_ticket(
    session: AsyncSession, assignee: User, status: TicketStatus = TicketStatus.in_progress
) -> Ticket:
    ticket = Ticket(
        author_id=assignee.id,
        assignee_id=assignee.id,
        description="role-service test ticket",
        priority=Priority.medium,
        request_id="REQ-ROLE",
        status=status,
        version=1,
        taken_at=datetime.now(UTC),
    )
    session.add(ticket)
    await session.flush()
    return ticket


async def test_admin_assigns_developer_to_roleless_user(session: AsyncSession) -> None:
    admin = await mk_user(session, 1001, Role.admin)
    target = await mk_user(session, 1002)
    await session.commit()

    updated = await assign_role(
        session, actor=admin, target_user_id=target.id, new_role=Role.developer
    )
    await session.commit()

    assert updated.role is Role.developer
    assert await fresh_role(session, target.id) is Role.developer


@pytest.mark.parametrize("role", [Role.support, None])
async def test_non_admin_cannot_assign(session: AsyncSession, role: Role | None) -> None:
    actor = await mk_user(session, 1001, role)
    target = await mk_user(session, 1002)
    await session.commit()

    with pytest.raises(PermissionDenied):
        await assign_role(session, actor=actor, target_user_id=target.id, new_role=Role.developer)

    assert await fresh_role(session, target.id) is None


async def test_assign_nonexistent_user_raises(session: AsyncSession) -> None:
    admin = await mk_user(session, 1001, Role.admin)
    await session.commit()

    with pytest.raises(UserNotFound):
        await assign_role(session, actor=admin, target_user_id=999999, new_role=Role.support)


async def test_assign_role_reads_fresh_role_not_identity_map_cache(
    session: AsyncSession, engine: AsyncEngine
) -> None:
    admin = await mk_user(session, 1001, Role.admin)
    target = await mk_user(session, 1002, Role.support)
    await session.commit()

    # Prime session A's identity map with the (soon stale) role=support.
    cached = await session.get(User, target.id)
    assert cached is not None and cached.role is Role.support

    # Session B upgrades the user to developer and hands them an active ticket.
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as other:
        target_b = await other.get(User, target.id)
        assert target_b is not None
        target_b.role = Role.developer
        await mk_active_ticket(other, target_b, TicketStatus.in_progress)
        await other.commit()

    with pytest.raises(RoleChangeBlocked):
        await assign_role(session, actor=admin, target_user_id=target.id, new_role=None)


async def test_downgrade_developer_to_support_without_active_tickets(session: AsyncSession) -> None:
    admin = await mk_user(session, 1001, Role.admin)
    developer = await mk_user(session, 1002, Role.developer)
    await session.commit()

    updated = await assign_role(
        session, actor=admin, target_user_id=developer.id, new_role=Role.support
    )
    await session.commit()

    assert updated.role is Role.support
    assert await fresh_role(session, developer.id) is Role.support


async def test_downgrade_developer_with_in_progress_ticket_blocked(session: AsyncSession) -> None:
    admin = await mk_user(session, 1001, Role.admin)
    developer = await mk_user(session, 1002, Role.developer)
    await session.commit()
    ticket = await mk_active_ticket(session, developer, TicketStatus.in_progress)
    await session.commit()

    with pytest.raises(RoleChangeBlocked) as exc_info:
        await assign_role(session, actor=admin, target_user_id=developer.id, new_role=Role.support)

    assert ticket.id in exc_info.value.active_ticket_ids
    assert await fresh_role(session, developer.id) is Role.developer


async def test_downgrade_admin_to_none_with_waiting_info_ticket_blocked(
    session: AsyncSession,
) -> None:
    admin_actor = await mk_user(session, 1001, Role.admin)
    admin_target = await mk_user(session, 1002, Role.admin)
    await session.commit()
    ticket = await mk_active_ticket(session, admin_target, TicketStatus.waiting_info)
    await session.commit()

    with pytest.raises(RoleChangeBlocked) as exc_info:
        await assign_role(session, actor=admin_actor, target_user_id=admin_target.id, new_role=None)

    assert exc_info.value.active_ticket_ids == [ticket.id]
    assert await fresh_role(session, admin_target.id) is Role.admin


async def test_upgrade_developer_to_admin_with_active_tickets(session: AsyncSession) -> None:
    admin = await mk_user(session, 1001, Role.admin)
    developer = await mk_user(session, 1002, Role.developer)
    await session.commit()
    await mk_active_ticket(session, developer, TicketStatus.in_progress)
    await session.commit()

    updated = await assign_role(
        session, actor=admin, target_user_id=developer.id, new_role=Role.admin
    )
    await session.commit()

    assert updated.role is Role.admin
    assert await fresh_role(session, developer.id) is Role.admin


async def test_admin_to_developer_with_active_tickets(session: AsyncSession) -> None:
    admin_actor = await mk_user(session, 1001, Role.admin)
    admin_target = await mk_user(session, 1002, Role.admin)
    await session.commit()
    await mk_active_ticket(session, admin_target, TicketStatus.in_progress)
    await session.commit()

    updated = await assign_role(
        session, actor=admin_actor, target_user_id=admin_target.id, new_role=Role.developer
    )
    await session.commit()

    assert updated.role is Role.developer
    assert await fresh_role(session, admin_target.id) is Role.developer
