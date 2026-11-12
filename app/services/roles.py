from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Role, Ticket, TicketStatus, User


class RoleChangeError(Exception):
    def __init__(self, message: str, *, active_ticket_ids: list[int] | None = None):
        super().__init__(message)
        self.active_ticket_ids = active_ticket_ids or []


async def change_user_role(
    session: AsyncSession, *, actor_id: int, target_id: int, role: Role | None
) -> User:
    actor = await session.scalar(select(User).where(User.id == actor_id).with_for_update(read=True))
    if actor is None or actor.role != Role.ADMIN:
        raise RoleChangeError("Нет доступа")
    target = await session.scalar(select(User).where(User.id == target_id).with_for_update())
    if target is None:
        raise RoleChangeError("Пользователь не найден")
    if target.role in (Role.DEVELOPER, Role.ADMIN) and role not in (Role.DEVELOPER, Role.ADMIN):
        active_ticket_ids = list(
            (
                await session.scalars(
                    select(Ticket.id).where(
                        Ticket.assignee_id == target_id,
                        Ticket.status.in_([TicketStatus.IN_PROGRESS, TicketStatus.WAITING_INFO]),
                    )
                )
            ).all()
        )
        if active_ticket_ids:
            raise RoleChangeError(
                "Сначала переназначьте активные обращения", active_ticket_ids=active_ticket_ids
            )
    target.role = role
    return target
