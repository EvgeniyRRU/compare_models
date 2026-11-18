"""REST API: /me, users, смена ролей."""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select

from app.api.deps import get_current_user, require_role
from app.api.schemas import MeOut, RoleRequest, UserOut
from app.log import get_logger
from app.models import User
from app.services import users as users_svc
from app.services.errors import ActiveTicketsError, NotFoundError

router = APIRouter()
log = get_logger("api.users")


@router.get("/me", response_model=MeOut)
async def me(user: User = Depends(get_current_user)) -> MeOut:
    return MeOut(
        id=user.id,
        telegram_id=user.telegram_id,
        first_name=user.first_name or "",
        username=user.username,
        role=user.role or "",
    )


@router.get("/users", response_model=list[UserOut])
async def list_users(
    request: Request,
    user: User = Depends(require_role("admin")),  # noqa: B008
) -> list[UserOut]:
    async with request.app.state.db.session_factory() as s:
        rows = (await s.execute(select(User).order_by(User.first_name, User.id))).scalars().all()
    return [
        UserOut(
            id=u.id,
            telegram_id=u.telegram_id,
            username=u.username,
            first_name=u.first_name,
            last_name=u.last_name,
            role=u.role,
        )
        for u in rows
    ]


@router.put("/users/{user_id}/role")
async def set_role(
    request: Request,
    user_id: int,
    body: RoleRequest,
    user: User = Depends(require_role("admin")),  # noqa: B008
) -> dict[str, object]:
    try:
        async with request.app.state.db.session_factory() as s, s.begin():
            u = await users_svc.change_role(s, user_id=user_id, new_role=body.role)
            return {"id": u.id, "role": u.role}
    except NotFoundError:
        raise HTTPException(404, "not found") from None
    except ActiveTicketsError as e:
        raise HTTPException(
            409, detail={"error": "active_tickets", "tickets": e.ticket_ids}
        ) from None
    except ValueError:
        raise HTTPException(422, "invalid role") from None
