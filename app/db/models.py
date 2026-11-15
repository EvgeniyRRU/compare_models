import enum
import uuid
from datetime import datetime

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
    Uuid,
    func,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Role(enum.StrEnum):
    support = "support"
    developer = "developer"
    admin = "admin"

    @property
    def can_execute(self) -> bool:
        """Admin implicitly has all developer+support rights."""
        return self in (Role.developer, Role.admin)


class Priority(enum.StrEnum):
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"


class TicketStatus(enum.StrEnum):
    new = "new"
    in_progress = "in_progress"
    waiting_info = "waiting_info"
    resolved = "resolved"
    rejected = "rejected"
    duplicate = "duplicate"

    @property
    def is_final(self) -> bool:
        return self in (TicketStatus.resolved, TicketStatus.rejected, TicketStatus.duplicate)


FINAL_STATUSES = (
    TicketStatus.resolved,
    TicketStatus.rejected,
    TicketStatus.duplicate,
)
ACTIVE_STATUSES = (TicketStatus.in_progress, TicketStatus.waiting_info)


class DraftStep(enum.StrEnum):
    description = "description"
    priority = "priority"
    request_id = "request_id"
    tenant_id = "tenant_id"
    confirm = "confirm"


class EventType(enum.StrEnum):
    created = "created"
    taken = "taken"
    status_changed = "status_changed"
    abandoned = "abandoned"
    reassigned = "reassigned"
    reopened = "reopened"


def pg_enum(python_enum: type[enum.Enum], name: str) -> SAEnum:
    """Native PostgreSQL enum with lowercase string values."""
    return SAEnum(
        python_enum,
        name=name,
        native_enum=True,
        create_type=False,
        values_callable=lambda e: [member.value for member in e],
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # updated_at is maintained exclusively by the DB trigger `trg_set_updated_at`
    # (one consistent mechanism for ORM and raw UPDATEs alike).
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64))
    full_name: Mapped[str] = mapped_column(String(256), server_default="")
    role: Mapped[Role | None] = mapped_column(pg_enum(Role, "role"))


class Ticket(TimestampMixin, Base):
    __tablename__ = "tickets"

    # Ticket number: GENERATED ALWAYS AS IDENTITY, displayed as #123.
    # Unique, never reused, gaps allowed.
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"), index=True)
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.telegram_id"), index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[Priority] = mapped_column(pg_enum(Priority, "priority"), nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[TicketStatus] = mapped_column(
        pg_enum(TicketStatus, "ticket_status"),
        nullable=False,
        default=TicketStatus.new,
        server_default=TicketStatus.new.value,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    post_synced_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    taken_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_reminder_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status <> 'new' OR (assignee_id IS NULL AND taken_at IS NULL)",
            name="ck_new_no_assignee",
        ),
        CheckConstraint(
            "status NOT IN ('in_progress', 'waiting_info') "
            "OR (assignee_id IS NOT NULL AND taken_at IS NOT NULL)",
            name="ck_active_has_assignee",
        ),
        CheckConstraint(
            "(closed_at IS NOT NULL) = (status IN ('resolved', 'rejected', 'duplicate'))",
            name="ck_closed_iff_final",
        ),
        Index("ix_tickets_new_due", "next_reminder_at", postgresql_where="status = 'new'"),
        Index(
            "ix_tickets_needs_post_sync",
            "id",
            postgresql_where="message_id IS NULL OR post_synced_version < version",
        ),
    )


class TicketEvent(Base):
    __tablename__ = "ticket_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True)
    event_type: Mapped[EventType] = mapped_column(pg_enum(EventType, "event_type"), nullable=False)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.telegram_id"))
    old_status: Mapped[TicketStatus | None] = mapped_column(pg_enum(TicketStatus, "ticket_status"))
    new_status: Mapped[TicketStatus | None] = mapped_column(pg_enum(TicketStatus, "ticket_status"))
    old_assignee_id: Mapped[int | None] = mapped_column(BigInteger)
    new_assignee_id: Mapped[int | None] = mapped_column(BigInteger)
    version_after: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TicketDraft(TimestampMixin, Base):
    __tablename__ = "ticket_drafts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # At most one active draft per user.
    user_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"), unique=True)
    step: Mapped[DraftStep] = mapped_column(pg_enum(DraftStep, "draft_step"), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[Priority | None] = mapped_column(pg_enum(Priority, "priority"))
    request_id: Mapped[str | None] = mapped_column(String(64))
    tenant_id: Mapped[str | None] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    prompt_message_id: Mapped[int | None] = mapped_column(BigInteger)


class ProcessedUpdate(Base):
    __tablename__ = "processed_updates"

    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
