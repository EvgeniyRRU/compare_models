"""Pydantic v2 schemas for the Mini App REST API."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.db.enums import Priority, Role, TicketStatus


class TicketOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    author_id: int
    assignee_id: int | None
    description: str
    priority: Priority
    request_id: str
    tenant_id: str | None
    status: TicketStatus
    version: int
    taken_at: datetime | None
    closed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class TicketListOut(BaseModel):
    items: list[TicketOut]
    total: int
    page: int
    page_size: int


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    telegram_id: int
    username: str | None
    full_name: str | None
    role: Role | None


class ReopenIn(BaseModel):
    version: int


class ReassignIn(BaseModel):
    version: int
    assignee_id: int


class SetRoleIn(BaseModel):
    role: Role | None


class RestorePostOut(BaseModel):
    ok: bool
