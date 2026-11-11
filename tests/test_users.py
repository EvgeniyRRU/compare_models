import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from support_bot.config import Settings
from support_bot.domain.errors import Forbidden, RoleChangeBlocked
from support_bot.domain.roles import Role
from support_bot.domain.state_machine import Action, Status
from support_bot.services import tickets as ticket_svc
from support_bot.services import users as svc
from tests.helpers import NOW, add_user


async def test_first_start_creates_user_without_role(
    session: AsyncSession, settings: Settings
) -> None:
    user = await svc.register_user(
        session, settings=settings, telegram_id=500, username="ivan", full_name="Иван"
    )
    await session.commit()

    assert user.role is None
    assert user.username == "ivan"


async def test_bootstrap_admin_gets_role_on_first_start(
    session: AsyncSession, settings: Settings
) -> None:
    settings.bootstrap_admin_ids = [777]

    user = await svc.register_user(
        session, settings=settings, telegram_id=777, username=None, full_name="Boss"
    )
    await session.commit()

    assert user.role == "admin"


async def test_repeated_start_refreshes_name_and_keeps_role(
    session: AsyncSession, settings: Settings
) -> None:
    await add_user(session, 600, "developer")

    user = await svc.register_user(
        session, settings=settings, telegram_id=600, username="new_nick", full_name="Новое имя"
    )
    await session.commit()

    assert user.role == "developer"
    assert user.username == "new_nick"
    assert user.full_name == "Новое имя"


async def test_only_admin_changes_roles(session: AsyncSession) -> None:
    developer = await add_user(session, 700, "developer")
    await add_user(session, 701, "developer")

    with pytest.raises(Forbidden):
        await svc.set_role(session, admin_id=developer, target_telegram_id=701, role=Role.SUPPORT)


async def test_role_change_is_blocked_while_user_holds_tickets(
    session: AsyncSession, settings: Settings
) -> None:
    support = await add_user(session, 800, "support")
    dev = await add_user(session, 801, "developer")
    admin = await add_user(session, 802, "admin")
    ticket = await ticket_svc.create_ticket(
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
    await ticket_svc.transition(
        session,
        settings=settings,
        actor_id=dev,
        ticket_id=ticket.id,
        action=Action.TAKE,
        expected_version=1,
        expected_status=Status.NEW,
        now=NOW,
    )
    await session.commit()

    with pytest.raises(RoleChangeBlocked) as info:
        await svc.set_role(session, admin_id=admin, target_telegram_id=801, role=Role.SUPPORT)

    assert info.value.ticket_ids == [ticket.id]
    await session.rollback()
    user = await svc.get_user_by_telegram_id(session, 801)
    assert user is not None and user.role == "developer"


async def test_role_change_is_allowed_after_reassignment(
    session: AsyncSession, settings: Settings
) -> None:
    support = await add_user(session, 810, "support")
    dev = await add_user(session, 811, "developer")
    new_dev = await add_user(session, 812, "developer")
    admin = await add_user(session, 813, "admin")
    ticket = await ticket_svc.create_ticket(
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
    await ticket_svc.transition(
        session,
        settings=settings,
        actor_id=dev,
        ticket_id=ticket.id,
        action=Action.TAKE,
        expected_version=1,
        expected_status=Status.NEW,
        now=NOW,
    )
    await session.commit()
    await ticket_svc.transition(
        session,
        settings=settings,
        actor_id=admin,
        ticket_id=ticket.id,
        action=Action.REASSIGN,
        expected_version=2,
        expected_status=Status.IN_PROGRESS,
        target_user_id=new_dev,
        now=NOW,
    )
    await session.commit()

    user = await svc.set_role(session, admin_id=admin, target_telegram_id=811, role=Role.SUPPORT)
    await session.commit()

    assert user.role == "support"


async def test_role_change_to_admin_or_developer_is_not_blocked(
    session: AsyncSession, settings: Settings
) -> None:
    await add_user(session, 820, "developer")
    admin = await add_user(session, 821, "admin")

    user = await svc.set_role(session, admin_id=admin, target_telegram_id=820, role=Role.ADMIN)
    await session.commit()

    assert user.role == "admin"
