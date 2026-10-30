import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.domain.state_machine import Priority, Role, Status


def _in(column: str, values: type[Role] | type[Status] | type[Priority]) -> str:
    return f"{column} IN ({', '.join(repr(v.value) for v in values)})"


class Base(DeclarativeBase):
    pass


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


def _updated() -> Mapped[datetime]:
    # Единый способ для всех таблиц: SQLAlchemy onupdate (работает и для Core UPDATE).
    return mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint(f"role IS NULL OR {_in('role', Role)}", name="users_role"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(256))
    last_name: Mapped[str | None] = mapped_column(String(256))
    role: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()

    @property
    def display_name(self) -> str:
        if self.username:
            return f"@{self.username}"
        return " ".join(p for p in (self.first_name, self.last_name) if p) or str(self.telegram_id)


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        CheckConstraint(_in("status", Status), name="tickets_status"),
        CheckConstraint(_in("priority", Priority), name="tickets_priority"),
        CheckConstraint(
            "status <> 'new' OR (assignee_id IS NULL AND taken_at IS NULL)",
            name="tickets_new_unassigned",
        ),
        CheckConstraint(
            "status NOT IN ('in_progress', 'waiting_info') "
            "OR (assignee_id IS NOT NULL AND taken_at IS NOT NULL)",
            name="tickets_active_assigned",
        ),
        CheckConstraint(
            "(status IN ('resolved', 'rejected', 'duplicate')) = (closed_at IS NOT NULL)",
            name="tickets_closed_at",
        ),
        CheckConstraint(
            "char_length(description) BETWEEN 1 AND 3000", name="tickets_description_len"
        ),
        CheckConstraint("char_length(request_id) BETWEEN 1 AND 64", name="tickets_request_id_len"),
        CheckConstraint("char_length(tenant_id) BETWEEN 1 AND 64", name="tickets_tenant_id_len"),
        Index("ix_tickets_status", "status"),
        Index("ix_tickets_author_id", "author_id"),
        Index("ix_tickets_assignee_id", "assignee_id"),
        Index(
            "ix_tickets_next_reminder",
            "next_reminder_at",
            postgresql_where=text("status = 'new'"),
        ),
        Index(
            "ix_tickets_unsynced",
            "id",
            postgresql_where=text("message_id IS NULL OR post_synced_version < version"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    description: Mapped[str] = mapped_column(String(3000))
    priority: Mapped[str] = mapped_column(String(16))
    request_id: Mapped[str] = mapped_column(String(64))
    tenant_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default=Status.NEW.value)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()
    taken_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_reminder_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    post_synced_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class TicketEvent(Base):
    __tablename__ = "ticket_events"
    __table_args__ = (Index("ix_ticket_events_ticket", "ticket_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"))
    event_type: Mapped[str] = mapped_column(String(32))
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    old_status: Mapped[str | None] = mapped_column(String(16))
    new_status: Mapped[str] = mapped_column(String(16))
    old_assignee_id: Mapped[int | None] = mapped_column(BigInteger)
    new_assignee_id: Mapped[int | None] = mapped_column(BigInteger)
    version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created()


class TicketDraft(Base):
    __tablename__ = "ticket_drafts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    step: Mapped[str] = mapped_column(String(16))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    prompt_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class ProcessedUpdate(Base):
    __tablename__ = "processed_updates"

    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    created_at: Mapped[datetime] = _created()
