"""Role service: admin-only role assignment with a downgrade guard.

Pure business logic — no Telegram or HTTP imports. The caller owns the
transaction: :func:`assign_role` never commits, and the caller (or a thin
wrapper) is responsible for the commit/rollback boundary.

User creation, ``/start`` registration, and telegram-id lookup live elsewhere
(Stage 5); this module only mutates an existing user's role.
"""

from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import Role, TicketStatus
from app.db.models import Ticket, User
from app.services.ticket_service import PermissionDenied


class UserNotFound(Exception):
    """The requested target user does not exist."""

    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        super().__init__("Пользователь не найден")


class RoleChangeBlocked(Exception):
    """A role downgrade would strand active tickets on a non-executor."""

    def __init__(self, user_id: int, active_ticket_ids: list[int]) -> None:
        self.user_id = user_id
        self.active_ticket_ids = active_ticket_ids
        super().__init__("Нельзя снять роль: есть активные обращения")


async def assign_role(
    session: AsyncSession,
    *,
    actor: User,
    target_user_id: int,
    new_role: Role | None,
) -> User:
    """Assign ``new_role`` to ``target_user_id``; ``Role.admin`` only.

    The actor's role is read fresh from the database (never cached). The target
    user row is locked ``FOR UPDATE`` so a concurrent role change cannot race the
    active-ticket guard below. Downgrades that strip executor capability
    (``developer``/``admin`` -> ``support``/``None``) are refused while the user
    has active tickets (``in_progress``/``waiting_info``).
    """
    actor_role = cast(
        Role | None,
        await session.scalar(select(User.role).where(User.id == actor.id)),
    )
    if actor_role is not Role.admin:
        raise PermissionDenied("assign_role")

    target = await session.scalar(select(User).where(User.id == target_user_id).with_for_update())
    if target is None:
        raise UserNotFound(target_user_id)

    old_role = target.role
    if old_role in (Role.developer, Role.admin) and new_role not in (
        Role.developer,
        Role.admin,
    ):
        result = await session.execute(
            select(Ticket.id)
            .where(
                Ticket.assignee_id == target_user_id,
                Ticket.status.in_((TicketStatus.in_progress, TicketStatus.waiting_info)),
            )
            .order_by(Ticket.id)
        )
        active_ticket_ids = list(result.scalars().all())
        if active_ticket_ids:
            raise RoleChangeBlocked(target_user_id, active_ticket_ids)

    target.role = new_role
    return target
