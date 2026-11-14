from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, Index, Integer, func
from sqlalchemy.orm import Mapped, mapped_column

from support_bot.db.base import Base
from support_bot.db.enums import TICKET_EVENT_TYPE, TICKET_STATUS, TicketEventType, TicketStatus


class TicketEvent(Base):
    __tablename__ = "ticket_events"
    __table_args__ = (Index("ix_ticket_events_ticket_created_at", "ticket_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ticket_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[TicketEventType] = mapped_column(TICKET_EVENT_TYPE, nullable=False)
    actor_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    old_status: Mapped[TicketStatus | None] = mapped_column(TICKET_STATUS, nullable=True)
    new_status: Mapped[TicketStatus | None] = mapped_column(TICKET_STATUS, nullable=True)
    old_assignee_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    new_assignee_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
