"""Pydantic-модели REST API Mini App."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class TicketOut(BaseModel):
    id: int
    description: str
    priority: str
    request_id: str
    tenant_id: str | None
    status: str
    version: int
    author_name: str
    assignee_name: str | None
    created_at: datetime
    updated_at: datetime
    taken_at: datetime | None
    closed_at: datetime | None


class TicketsPage(BaseModel):
    items: list[TicketOut]
    total: int
    page: int
    per_page: int


class ReopenRequest(BaseModel):
    expected_version: int


class ReassignRequest(BaseModel):
    user_id: int
    expected_version: int


class RoleRequest(BaseModel):
    role: str | None


class UserOut(BaseModel):
    id: int
    telegram_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    role: str | None


class MeOut(BaseModel):
    id: int
    telegram_id: int
    first_name: str | None
    username: str | None
    role: str
