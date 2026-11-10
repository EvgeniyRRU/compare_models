from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Ticket, User
from app.services.tickets import Forbidden


class ActiveTickets(Exception):
    def __init__(self, ticket_ids: list[int]):
        self.ticket_ids = ticket_ids


async def set_role(session: AsyncSession, actor_id: int, user_id: int, role: str | None) -> None:
    if role not in {None, "support", "developer", "admin"}:
        raise ValueError("Unknown role")
    # Lock in a stable order to avoid deadlocks when administrators edit each other.
    users = (
        await session.scalars(
            select(User).where(User.id.in_([actor_id, user_id])).order_by(User.id).with_for_update()
        )
    ).all()
    by_id = {user.id: user for user in users}
    if actor_id not in by_id or by_id[actor_id].role != "admin":
        raise Forbidden()
    if user_id not in by_id:
        raise LookupError("User not found")
    target = by_id[user_id]
    if target.role in {"developer", "admin"} and role not in {"developer", "admin"}:
        ids = list(
            (
                await session.scalars(
                    select(Ticket.id).where(
                        Ticket.assignee_id == user_id,
                        Ticket.status.in_(["in_progress", "waiting_info"]),
                    )
                )
            ).all()
        )
        if ids:
            raise ActiveTickets(ids)
    target.role = role
