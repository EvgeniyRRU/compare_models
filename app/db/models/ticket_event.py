"""``ticket_events`` table: audit journal of business changes to tickets."""

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
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TicketEvent(Base):
    __tablename__ = "ticket_events"

    __table_args__ = (
        CheckConstraint(
            "event_type IN ('created', 'taken', 'status_changed', "
            "'abandoned', 'reassigned', 'reopened')",
            name="event_type_valid",
        ),
        Index("ix_ticket_events_ticket_id_id", "ticket_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ticket_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("tickets.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    actor_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    old_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    new_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    old_assignee_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    new_assignee_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
