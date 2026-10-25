from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from support_bot.domain.enums import Priority, Role, TicketStatus
from support_bot.domain.state_machine import can_be_assignee, can_create, is_admin
from support_bot.services.views import TicketPage, TicketView, UserInfo


class Scope(StrEnum):
    ALL = "all"
    CREATED = "created"  # созданные мной
    ASSIGNED = "assigned"  # назначенные мне


class UserOut(BaseModel):
    id: int
    telegram_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    display_name: str
    role: Role | None

    @classmethod
    def of(cls, u: UserInfo) -> UserOut:
        return cls(
            id=u.id,
            telegram_id=u.telegram_id,
            username=u.username,
            first_name=u.first_name,
            last_name=u.last_name,
            display_name=u.display_name,
            role=u.role,
        )


class MeOut(UserOut):
    can_create: bool
    can_be_assignee: bool
    is_admin: bool

    @classmethod
    def of(cls, u: UserInfo) -> MeOut:
        return cls(
            **UserOut.of(u).model_dump(),
            can_create=can_create(u.role),
            can_be_assignee=can_be_assignee(u.role),
            is_admin=is_admin(u.role),
        )


class TicketOut(BaseModel):
    id: int
    status: TicketStatus
    priority: Priority
    description: str
    request_id: str
    tenant_id: str | None
    author: UserOut
    assignee: UserOut | None
    version: int
    created_at: datetime
    updated_at: datetime
    taken_at: datetime | None
    closed_at: datetime | None
    has_post: bool

    @classmethod
    def of(cls, v: TicketView) -> TicketOut:
        return cls(
            id=v.id,
            status=v.status,
            priority=v.priority,
            description=v.description,
            request_id=v.request_id,
            tenant_id=v.tenant_id,
            author=UserOut.of(v.author),
            assignee=UserOut.of(v.assignee) if v.assignee else None,
            version=v.version,
            created_at=v.created_at,
            updated_at=v.updated_at,
            taken_at=v.taken_at,
            closed_at=v.closed_at,
            has_post=v.message_id is not None,
        )


class TicketPermissions(BaseModel):
    can_reopen: bool
    can_reassign: bool
    can_restore_post: bool


class TicketDetailOut(TicketOut):
    permissions: TicketPermissions


class TicketPageOut(BaseModel):
    items: list[TicketOut]
    total: int
    page: int
    page_size: int

    @classmethod
    def of(cls, p: TicketPage) -> TicketPageOut:
        return cls(
            items=[TicketOut.of(v) for v in p.items],
            total=p.total,
            page=p.page,
            page_size=p.page_size,
        )


class VersionIn(BaseModel):
    version: int = Field(ge=1)


class ReassignIn(VersionIn):
    assignee_id: int


class RoleIn(BaseModel):
    role: Role | None


class ErrorOut(BaseModel):
    error: str
    detail: str
    tickets: list[int] | None = None
