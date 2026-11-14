from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from support_bot.db.enums import TicketPriority, TicketStatus, UserRole


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    telegram_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    role: UserRole | None


class TicketOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    author: UserOut
    assignee: UserOut | None
    description: str
    priority: TicketPriority
    request_id: str
    tenant_id: str | None
    status: TicketStatus
    version: int
    created_at: datetime
    updated_at: datetime
    taken_at: datetime | None
    closed_at: datetime | None
    next_reminder_at: datetime | None
    message_id: int | None


class TicketListOut(BaseModel):
    items: list[TicketOut]
    total: int
    page: int
    page_size: int


class UserListOut(BaseModel):
    items: list[UserOut]
    total: int
    page: int
    page_size: int


class MeOut(BaseModel):
    user: UserOut
    is_admin: bool
    can_create: bool
    can_take: bool


class VersionActionIn(BaseModel):
    version: int = Field(ge=1)


class ReassignIn(BaseModel):
    version: int = Field(ge=1)
    assignee_id: int


class RoleUpdateIn(BaseModel):
    role: UserRole | None = None
