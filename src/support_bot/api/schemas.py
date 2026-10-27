from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from support_bot.domain.enums import Priority, Role, Status


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    telegram_id: int
    username: str | None
    first_name: str
    last_name: str | None
    role: Role | None
    created_at: datetime
    updated_at: datetime


class TicketOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    author_id: int
    assignee_id: int | None
    description: str
    priority: Priority
    request_id: str
    tenant_id: str | None
    status: Status
    version: int
    created_at: datetime
    updated_at: datetime
    taken_at: datetime | None
    closed_at: datetime | None
    next_reminder_at: datetime | None
    message_id: int | None
    post_synced_version: int
    author: UserOut | None = None
    assignee: UserOut | None = None


class TicketPage(BaseModel):
    items: list[TicketOut]
    total: int
    page: int
    page_size: int


class UserPage(BaseModel):
    items: list[UserOut]
    total: int
    page: int
    page_size: int


class ChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    expected_status: Status


class ReassignRequest(ChangeRequest):
    assignee_id: int = Field(gt=0)


class RoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Role | None


Scope = Literal["all", "created", "assigned"]
