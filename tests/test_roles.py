from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from support_bot.db.enums import UserRole
from support_bot.db.models.user import User
from support_bot.domain.errors import PermissionDenied, RoleChangeConflict
from support_bot.domain.state_machine import TicketAction
from support_bot.services import tickets as tickets_service
from support_bot.services import users as users_service


async def test_admin_can_change_role(session_factory, make_user) -> None:
    admin = await make_user(telegram_id=100, role=UserRole.ADMIN)
    user = await make_user(telegram_id=101, role=None)
    async with session_factory() as session:
        updated = await users_service.set_role(
            session,
            actor_id=admin.id,
            target_id=user.id,
            new_role=UserRole.DEVELOPER,
        )
    assert updated.role is UserRole.DEVELOPER


async def test_non_admin_cannot_change_role(session_factory, make_user) -> None:
    developer = await make_user(telegram_id=102, role=UserRole.DEVELOPER)
    user = await make_user(telegram_id=103, role=None)
    async with session_factory() as session:
        with pytest.raises(PermissionDenied):
            await users_service.set_role(
                session,
                actor_id=developer.id,
                target_id=user.id,
                new_role=UserRole.SUPPORT,
            )


async def test_cannot_demote_executor_with_active_tickets(
    session_factory, make_user, make_ticket
) -> None:
    admin = await make_user(telegram_id=104, role=UserRole.ADMIN)
    author = await make_user(telegram_id=105, role=UserRole.SUPPORT)
    developer = await make_user(telegram_id=106, role=UserRole.DEVELOPER)
    ticket = await make_ticket(author_id=author.id)

    async with session_factory() as session:
        await tickets_service.take_ticket(
            session, ticket_id=ticket.id, expected_version=1, actor_id=developer.id
        )

    async with session_factory() as session:
        with pytest.raises(RoleChangeConflict) as exc:
            await users_service.set_role(
                session,
                actor_id=admin.id,
                target_id=developer.id,
                new_role=UserRole.SUPPORT,
            )
    assert exc.value.ticket_ids == [ticket.id]

    async with session_factory() as session:
        await tickets_service.change_status(
            session,
            ticket_id=ticket.id,
            expected_version=2,
            action=TicketAction.RESOLVE,
            actor_id=developer.id,
            policy=_policy(),
        )

    async with session_factory() as session:
        updated = await users_service.set_role(
            session,
            actor_id=admin.id,
            target_id=developer.id,
            new_role=None,
        )
    assert updated.role is None


async def test_demote_waiting_info_is_blocked(session_factory, make_user, make_ticket) -> None:
    admin = await make_user(telegram_id=107, role=UserRole.ADMIN)
    author = await make_user(telegram_id=108, role=UserRole.SUPPORT)
    developer = await make_user(telegram_id=109, role=UserRole.DEVELOPER)
    ticket = await make_ticket(author_id=author.id)

    async with session_factory() as session:
        await tickets_service.take_ticket(
            session, ticket_id=ticket.id, expected_version=1, actor_id=developer.id
        )
    async with session_factory() as session:
        await tickets_service.change_status(
            session,
            ticket_id=ticket.id,
            expected_version=2,
            action=TicketAction.WAIT_INFO,
            actor_id=developer.id,
            policy=_policy(),
        )
    async with session_factory() as session:
        with pytest.raises(RoleChangeConflict):
            await users_service.set_role(
                session,
                actor_id=admin.id,
                target_id=developer.id,
                new_role=None,
            )


async def test_role_change_blocks_on_share_lock(session_factory, make_user) -> None:
    """Take holds FOR SHARE on the executor row; role change must wait for it."""
    admin = await make_user(telegram_id=110, role=UserRole.ADMIN)
    developer = await make_user(telegram_id=111, role=UserRole.DEVELOPER)

    async with session_factory() as locker, locker.begin():
        await locker.execute(select(User).where(User.id == developer.id).with_for_update(read=True))
        task = asyncio.create_task(
            _set_role_in_new_session(session_factory, admin.id, developer.id, None)
        )
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), 0.3)
    updated = await task
    assert updated.role is None


async def _set_role_in_new_session(session_factory, actor_id: int, target_id: int, role):
    async with session_factory() as session:
        return await users_service.set_role(
            session, actor_id=actor_id, target_id=target_id, new_role=role
        )


def _policy():
    from support_bot.config import Settings
    from support_bot.domain.reminders import ReminderPolicy

    return ReminderPolicy.from_settings(Settings())
