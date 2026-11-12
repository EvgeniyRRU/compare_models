from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Role(StrEnum):
    SUPPORT = "support"
    DEVELOPER = "developer"
    ADMIN = "admin"


class TicketStatus(StrEnum):
    NEW = "new"
    IN_PROGRESS = "in_progress"
    WAITING_INFO = "waiting_info"
    RESOLVED = "resolved"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"


class Priority(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    full_name: Mapped[str] = mapped_column(String(256), default="")
    role: Mapped[Role | None] = mapped_column(
        Enum(Role, name="user_role", values_callable=lambda e: [x.value for x in e])
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ticket_version_positive"),
        CheckConstraint(
            "(status = 'new' AND assignee_id IS NULL AND taken_at IS NULL) OR "
            "(status IN ('in_progress','waiting_info') AND assignee_id IS NOT NULL "
            "AND taken_at IS NOT NULL) OR "
            "(status IN ('resolved','rejected','duplicate') AND "
            "((assignee_id IS NULL AND taken_at IS NULL) OR "
            "(assignee_id IS NOT NULL AND taken_at IS NOT NULL)))",
            name="ticket_assignment_consistent",
        ),
        CheckConstraint(
            "(status IN ('resolved','rejected','duplicate')) = (closed_at IS NOT NULL)",
            name="ticket_closed_consistent",
        ),
        Index("ix_tickets_pending_sync", "post_synced_version", "version"),
        Index("ix_tickets_due_reminder", "next_reminder_at"),
    )
    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    description: Mapped[str] = mapped_column(String(3000))
    priority: Mapped[Priority] = mapped_column(
        Enum(Priority, name="ticket_priority", values_callable=lambda e: [x.value for x in e])
    )
    request_id: Mapped[str] = mapped_column(String(64))
    tenant_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[TicketStatus] = mapped_column(
        Enum(TicketStatus, name="ticket_status", values_callable=lambda e: [x.value for x in e]),
        default=TicketStatus.NEW,
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    taken_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_reminder_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    post_synced_version: Mapped[int] = mapped_column(Integer, default=0)
    author: Mapped[User] = relationship(foreign_keys=[author_id])
    assignee: Mapped[User | None] = relationship(foreign_keys=[assignee_id])


class TicketEvent(Base):
    __tablename__ = "ticket_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), index=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(32))
    old_status: Mapped[TicketStatus | None] = mapped_column(
        Enum(TicketStatus, name="event_old_status", values_callable=lambda e: [x.value for x in e])
    )
    new_status: Mapped[TicketStatus] = mapped_column(
        Enum(TicketStatus, name="event_new_status", values_callable=lambda e: [x.value for x in e])
    )
    old_assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    new_assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TicketDraft(Base):
    __tablename__ = "ticket_drafts"
    __table_args__ = (CheckConstraint("version >= 1", name="draft_version_positive"),)
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, index=True)
    step: Mapped[str] = mapped_column(String(24), default="description")
    description: Mapped[str | None] = mapped_column(String(3000))
    priority: Mapped[Priority | None] = mapped_column(
        Enum(Priority, name="draft_priority", values_callable=lambda e: [x.value for x in e])
    )
    request_id: Mapped[str | None] = mapped_column(String(64))
    tenant_id: Mapped[str | None] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    prompt_message_id: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ProcessedUpdate(Base):
    __tablename__ = "processed_updates"
    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
