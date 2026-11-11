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
    MetaData,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

ROLES = ("support", "developer", "admin")
PRIORITIES = ("critical", "high", "medium", "low")
STATUSES = ("new", "in_progress", "waiting_info", "resolved", "rejected", "duplicate")
FINAL_STATUSES = ("resolved", "rejected", "duplicate")
ASSIGNED_STATUSES = ("in_progress", "waiting_info")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Every table with updated_at uses the SQLAlchemy onupdate hook, including Core updates.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint(_in("role", ROLES) + " OR role IS NULL", "role"),)

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    username: Mapped[str | None] = mapped_column(String(64))
    full_name: Mapped[str] = mapped_column(String(256))
    role: Mapped[str | None] = mapped_column(String(16))


class Ticket(TimestampMixin, Base):
    __tablename__ = "tickets"
    __table_args__ = (
        CheckConstraint(_in("status", STATUSES), "status"),
        CheckConstraint(_in("priority", PRIORITIES), "priority"),
        CheckConstraint("version >= 1", "version_positive"),
        CheckConstraint(
            "status <> 'new' OR (assignee_id IS NULL AND taken_at IS NULL)",
            "new_is_unassigned",
        ),
        CheckConstraint(
            f"NOT ({_in('status', ASSIGNED_STATUSES)}) "
            "OR (assignee_id IS NOT NULL AND taken_at IS NOT NULL)",
            "assigned_has_taker",
        ),
        CheckConstraint(
            f"(closed_at IS NOT NULL) = ({_in('status', FINAL_STATUSES)})",
            "closed_at_matches_final",
        ),
        CheckConstraint(
            "status = 'new' OR next_reminder_at IS NULL",
            "reminder_only_for_new",
        ),
    )

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    description: Mapped[str] = mapped_column(String(3000))
    priority: Mapped[str] = mapped_column(String(16))
    request_id: Mapped[str] = mapped_column(String(64))
    tenant_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="new", server_default="new")
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    taken_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_reminder_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    post_synced_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class TicketEvent(Base):
    __tablename__ = "ticket_events"
    __table_args__ = (Index("ix_ticket_events_ticket_id_id", "ticket_id", "id"),)

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"))
    event_type: Mapped[str] = mapped_column(String(32))
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16))
    from_assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    to_assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TicketDraft(TimestampMixin, Base):
    __tablename__ = "ticket_drafts"
    __table_args__ = (
        CheckConstraint(
            _in("step", ("description", "priority", "request_id", "tenant_id", "confirm")),
            "step",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    step: Mapped[str] = mapped_column(
        String(16), default="description", server_default="description"
    )
    description: Mapped[str | None] = mapped_column(String(3000))
    priority: Mapped[str | None] = mapped_column(String(16))
    request_id: Mapped[str | None] = mapped_column(String(64))
    tenant_id: Mapped[str | None] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    prompt_message_id: Mapped[int | None] = mapped_column(BigInteger)


class ProcessedUpdate(Base):
    __tablename__ = "processed_updates"

    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
