from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from support_bot.db.base import Base
from support_bot.db.enums import (
    TICKET_PRIORITY,
    TICKET_STATUS,
    TicketPriority,
    TicketStatus,
)
from support_bot.db.models.user import User


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
            "tenant_id IS NULL OR char_length(tenant_id) BETWEEN 1 AND 64",
            name="tenant_id_length",
        ),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("post_synced_version >= 0", name="post_synced_version_non_negative"),
        CheckConstraint(
            "(status = 'new' AND assignee_id IS NULL AND taken_at IS NULL) "
            "OR (status <> 'new' AND assignee_id IS NOT NULL AND taken_at IS NOT NULL)",
            name="assignee_matches_status",
        ),
        CheckConstraint(
            "(status IN ('resolved', 'rejected', 'duplicate')) = (closed_at IS NOT NULL)",
            name="closed_at_matches_status",
        ),
        CheckConstraint(
            "(status = 'new') = (next_reminder_at IS NOT NULL)",
            name="reminder_matches_status",
        ),
        Index("ix_tickets_status_created_at", "status", "created_at"),
        Index("ix_tickets_author_created_at", "author_id", "created_at"),
        Index("ix_tickets_assignee_created_at", "assignee_id", "created_at"),
        Index(
            "ix_tickets_post_sync",
            "id",
            postgresql_where=text("message_id IS NULL OR post_synced_version < version"),
        ),
        Index(
            "ix_tickets_due_reminder",
            "next_reminder_at",
            postgresql_where=text("status = 'new'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    author_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    assignee_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[TicketPriority] = mapped_column(TICKET_PRIORITY, nullable=False)
    request_id: Mapped[str] = mapped_column(Text, nullable=False)
    tenant_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[TicketStatus] = mapped_column(
        TICKET_STATUS, nullable=False, server_default=text("'new'")
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1"), default=1
    )
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
        Integer, nullable=False, server_default=text("0"), default=0
    )

    author: Mapped[User] = relationship(foreign_keys=[author_id], lazy="raise")
    assignee: Mapped[User | None] = relationship(foreign_keys=[assignee_id], lazy="raise")
