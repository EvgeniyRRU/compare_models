from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at_column


class TicketEvent(Base):
    __tablename__ = "ticket_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('created', 'taken', 'status_changed', "
            "'declined', 'reassigned', 'reopened')",
            name="ck_ticket_events_type",
        ),
        Index("ix_ticket_events_ticket_id", "ticket_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(Integer, ForeignKey("tickets.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    old_status: Mapped[str | None] = mapped_column(String(16))
    new_status: Mapped[str | None] = mapped_column(String(16))
    old_assignee_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))
    new_assignee_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))
    version: Mapped[int] = mapped_column(Integer, nullable=False)  # version после изменения
    created_at: Mapped[datetime] = created_at_column()
