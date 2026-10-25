import asyncio

import pytest

from support_bot.domain.enums import Role
from support_bot.domain.errors import (
    AlreadyTakenError,
    PermissionDeniedError,
    RoleChangeBlockedError,
    ValidationFailedError,
)
from support_bot.domain.state_machine import Action
from support_bot.services.tickets import TicketService
from support_bot.services.users import TelegramProfile, UserService
from tests.conftest import MakeTicket, MakeUser


async def test_start_registers_without_role(users: UserService) -> None:
    u = await users.register_start(TelegramProfile(1, "ivan", "Иван", None))
    assert u.role is None and u.username == "ivan"
    u2 = await users.touch(TelegramProfile(1, "ivan2", "Иван", "Петров"))
    assert u2.id == u.id and u2.username == "ivan2" and u2.last_name == "Петров"


async def test_bootstrap_admin(users: UserService) -> None:
    u = await users.register_start(TelegramProfile(999, None, "Boss", None))
    assert u.role is Role.ADMIN
    # touch без /start роль не выдаёт
    other = await users.touch(TelegramProfile(1000, None, "X", None))
    assert other.role is None


async def test_bootstrap_does_not_override_existing_role(
    users: UserService, make_user: MakeUser
) -> None:
    await make_user(Role.DEVELOPER, telegram_id=999)
    u = await users.register_start(TelegramProfile(999, None, "Boss", None))
    assert u.role is Role.DEVELOPER


async def test_set_role_admin_only(users: UserService, make_user: MakeUser) -> None:
    dev, target = await make_user(Role.DEVELOPER), await make_user(None)
    with pytest.raises(PermissionDeniedError):
        await users.set_role(dev.id, target.id, Role.SUPPORT)
    admin = await make_user(Role.ADMIN)
    assert (await users.set_role(admin.id, target.id, Role.SUPPORT)).role is Role.SUPPORT


async def test_cannot_change_own_role_or_last_admin(
    users: UserService, make_user: MakeUser
) -> None:
    admin = await make_user(Role.ADMIN)
    with pytest.raises(ValidationFailedError):
        await users.set_role(admin.id, admin.id, Role.DEVELOPER)
    admin2 = await make_user(Role.ADMIN)
    await users.set_role(admin.id, admin2.id, Role.DEVELOPER)


async def test_role_change_blocked_with_active_tickets(
    users: UserService, tickets: TicketService, make_user: MakeUser, make_ticket: MakeTicket
) -> None:
    support, dev, admin = (
        await make_user(Role.SUPPORT),
        await make_user(Role.DEVELOPER),
        await make_user(Role.ADMIN),
    )
    t1, t2 = await make_ticket(support), await make_ticket(support)
    await tickets.apply_action(dev.id, t1, Action.TAKE, 1)
    await tickets.apply_action(dev.id, t2, Action.TAKE, 1)
    await tickets.apply_action(dev.id, t2, Action.TO_WAITING, 2)

    for role in (Role.SUPPORT, None):
        with pytest.raises(RoleChangeBlockedError) as exc_info:
            await users.set_role(admin.id, dev.id, role)
        assert exc_info.value.ticket_ids == [t1, t2]
    # developer -> admin сохраняет право быть исполнителем
    await users.set_role(admin.id, dev.id, Role.ADMIN)
    with pytest.raises(RoleChangeBlockedError):
        await users.set_role(admin.id, dev.id, Role.SUPPORT)

    # после закрытия обращений смена роли проходит
    await tickets.apply_action(dev.id, t1, Action.RESOLVE, 2)
    await tickets.apply_action(dev.id, t2, Action.RELEASE, 3)
    assert (await users.set_role(admin.id, dev.id, Role.SUPPORT)).role is Role.SUPPORT


async def test_role_change_vs_concurrent_take(
    users: UserService, tickets: TicketService, make_user: MakeUser, make_ticket: MakeTicket
) -> None:
    """Параллельные взятие и снятие роли: не может быть active-обращения у support."""
    for _ in range(10):
        support, admin = await make_user(Role.SUPPORT), await make_user(Role.ADMIN)
        dev = await make_user(Role.DEVELOPER)
        tid = await make_ticket(support)
        take, change = await asyncio.gather(
            tickets.apply_action(dev.id, tid, Action.TAKE, 1),
            users.set_role(admin.id, dev.id, Role.SUPPORT),
            return_exceptions=True,
        )
        took = not isinstance(take, BaseException)
        changed = not isinstance(change, BaseException)
        assert took != changed, (take, change)
        if not took:
            assert isinstance(take, PermissionDeniedError) and not isinstance(
                take, AlreadyTakenError
            )
        else:
            assert isinstance(change, RoleChangeBlockedError)
