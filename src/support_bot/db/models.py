"""PostgreSQL schema. Timestamps on updates are maintained by migration triggers."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from support_bot.db.base import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "role IS NULL OR role IN ('support','developer','admin')", name="ck_user_role"
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    username: Mapped[str | None] = mapped_column(String(255))
    first_name: Mapped[str] = mapped_column(String(255))
    last_name: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str | None] = mapped_column(String(16))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), server_onupdate=func.now()
    )


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        CheckConstraint(
            "status IN ('new','in_progress','waiting_info','resolved','rejected','duplicate')",
            name="ck_ticket_status",
        ),
        CheckConstraint(
            "priority IN ('critical','high','medium','low')", name="ck_ticket_priority"
        ),
        CheckConstraint(
            "length(btrim(description)) > 0 AND length(description) <= 3000",
            name="ck_ticket_description",
        ),
        CheckConstraint(
            "length(btrim(request_id)) > 0 AND length(request_id) <= 64", name="ck_ticket_request"
        ),
        CheckConstraint(
            "tenant_id IS NULL OR (length(btrim(tenant_id)) > 0 AND length(tenant_id) <= 64)",
            name="ck_ticket_tenant",
        ),
        CheckConstraint(
            "version >= 1 AND post_synced_version >= 0 AND post_synced_version <= version",
            name="ck_ticket_version",
        ),
        CheckConstraint(
            "(status != 'new' OR (assignee_id IS NULL AND taken_at IS NULL)) "
            "AND (status NOT IN ('in_progress','waiting_info') "
            "OR (assignee_id IS NOT NULL AND taken_at IS NOT NULL))",
            name="ck_ticket_assignment",
        ),
        CheckConstraint(
            "(status IN ('resolved','rejected','duplicate')) = (closed_at IS NOT NULL)",
            name="ck_ticket_closed",
        ),
        CheckConstraint(
            "(status = 'new') = (next_reminder_at IS NOT NULL)", name="ck_ticket_reminder"
        ),
        Index("ix_ticket_author_date", "author_id", "created_at", "id"),
        Index("ix_ticket_assignee_date", "assignee_id", "created_at", "id"),
        Index("ix_ticket_filter_date", "status", "priority", "created_at", "id"),
        Index("ix_ticket_due", "next_reminder_at", postgresql_where=text("status = 'new'")),
        Index(
            "ix_ticket_unsynced",
            "id",
            postgresql_where=text("message_id IS NULL OR post_synced_version < version"),
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    description: Mapped[str] = mapped_column(Text)
    priority: Mapped[str] = mapped_column(String(16))
    request_id: Mapped[str] = mapped_column(String(64))
    tenant_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), server_default="new")
    version: Mapped[int] = mapped_column(Integer, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), server_onupdate=func.now()
    )
    taken_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_reminder_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    post_synced_version: Mapped[int] = mapped_column(Integer, server_default="0")


class TicketEvent(Base):
    __tablename__ = "ticket_events"
    __table_args__ = (
        UniqueConstraint("ticket_id", "version", name="uq_ticket_event_version"),
        CheckConstraint(
            "event_type IN ('created','taken','status_changed','refused','reassigned','reopened')",
            name="ck_event_type",
        ),
        Index("ix_ticket_event_history", "ticket_id", "created_at", "id"),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"))
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    event_type: Mapped[str] = mapped_column(String(24))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    old_status: Mapped[str | None] = mapped_column(String(16))
    new_status: Mapped[str] = mapped_column(String(16))
    old_assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    new_assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    version: Mapped[int] = mapped_column(Integer)


class TicketDraft(Base):
    __tablename__ = "ticket_drafts"
    __table_args__ = (
        CheckConstraint(
            "step IN ('description','priority','request_id','tenant_id','confirm')",
            name="ck_draft_step",
        ),
        CheckConstraint("version >= 1", name="ck_draft_version"),
        Index("ix_draft_updated", "updated_at"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    step: Mapped[str] = mapped_column(String(16), server_default="description")
    description: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[str | None] = mapped_column(String(16))
    request_id: Mapped[str | None] = mapped_column(String(64))
    tenant_id: Mapped[str | None] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, server_default="1")
    prompt_message_id: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), server_onupdate=func.now()
    )


class ProcessedUpdate(Base):
    __tablename__ = "processed_updates"
    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
