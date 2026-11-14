"""``tickets`` table: support tickets and their lifecycle state."""

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
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.enums import Priority, TicketStatus, str_enum


class Ticket(Base):
    __tablename__ = "tickets"

    __table_args__ = (
        CheckConstraint(
            "char_length(description) BETWEEN 1 AND 3000",
            name="description_length",
        ),
        CheckConstraint(
            "char_length(request_id) BETWEEN 1 AND 64",
            name="request_id_length",
        ),
        CheckConstraint(
            "tenant_id IS NULL OR char_length(tenant_id) <= 64",
            name="tenant_id_length",
        ),
        CheckConstraint(
            "status <> 'new' OR (assignee_id IS NULL AND taken_at IS NULL)",
            name="new_has_no_assignee",
        ),
        CheckConstraint(
            "status NOT IN ('in_progress', 'waiting_info') "
            "OR (assignee_id IS NOT NULL AND taken_at IS NOT NULL)",
            name="active_has_assignee",
        ),
        CheckConstraint(
            "(status IN ('resolved', 'rejected', 'duplicate')) = (closed_at IS NOT NULL)",
            name="closed_at_matches_final",
        ),
        Index("ix_tickets_status_next_reminder_at", "status", "next_reminder_at"),
        Index("ix_tickets_author_id", "author_id"),
        Index("ix_tickets_assignee_id", "assignee_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    author_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    assignee_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[Priority] = mapped_column(str_enum(Priority), nullable=False)
    # ``request_id`` is intentionally NOT unique: the spec only requires non-empty
    # and length <= 64, so duplicate external reference ids are allowed.
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[TicketStatus] = mapped_column(str_enum(TicketStatus), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    taken_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_reminder_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    post_synced_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
