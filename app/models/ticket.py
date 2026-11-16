from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at_column, updated_at_column


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        CheckConstraint(
            "length(description) BETWEEN 1 AND 3000", name="ck_tickets_description_len"
        ),
        CheckConstraint(
            "priority IN ('critical', 'high', 'medium', 'low')", name="ck_tickets_priority"
        ),
        CheckConstraint("length(request_id) BETWEEN 1 AND 64", name="ck_tickets_request_id_len"),
        CheckConstraint(
            "tenant_id IS NULL OR length(tenant_id) BETWEEN 1 AND 64",
            name="ck_tickets_tenant_id_len",
        ),
        CheckConstraint(
            "status IN ('new', 'in_progress', 'waiting_info', 'resolved', 'rejected', 'duplicate')",
            name="ck_tickets_status",
        ),
        CheckConstraint("version >= 1", name="ck_tickets_version"),
        CheckConstraint("post_synced_version >= 0", name="ck_tickets_post_synced_version"),
        # Инварианты (SPEC «Сущности и статусы»)
        CheckConstraint(
            "status <> 'new' OR (assignee_id IS NULL AND taken_at IS NULL)",
            name="ck_tickets_new_unassigned",
        ),
        CheckConstraint(
            "status NOT IN ('in_progress', 'waiting_info') OR assignee_id IS NOT NULL",
            name="ck_tickets_active_requires_assignee",
        ),
        CheckConstraint(
            "status NOT IN ('in_progress', 'waiting_info') OR taken_at IS NOT NULL",
            name="ck_tickets_active_requires_taken_at",
        ),
        CheckConstraint(
            "status IN ('resolved', 'rejected', 'duplicate') OR closed_at IS NULL",
            name="ck_tickets_closed_at_only_final",
        ),
        CheckConstraint(
            "status NOT IN ('resolved', 'rejected', 'duplicate') OR closed_at IS NOT NULL",
            name="ck_tickets_closed_at_required_final",
        ),
        Index("ix_tickets_status_next_reminder", "status", "next_reminder_at"),
        Index("ix_tickets_author_id", "author_id"),
        Index("ix_tickets_assignee_id", "assignee_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    author_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    assignee_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))
    description: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[str] = mapped_column(String(16), nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="new", server_default="new"
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    post_synced_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()
    taken_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_reminder_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
