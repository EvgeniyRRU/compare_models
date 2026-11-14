"""``ticket_events`` table: audit journal of business changes to tickets."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
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
from app.db.enums import TicketEventType, str_enum


class TicketEvent(Base):
    __tablename__ = "ticket_events"

    __table_args__ = (Index("ix_ticket_events_ticket_id_id", "ticket_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ticket_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tickets.id", ondelete="RESTRICT"), nullable=False
    )
    event_type: Mapped[TicketEventType] = mapped_column(
        str_enum(TicketEventType, length=30), nullable=False
    )
    actor_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    # ``old_*``/``new_*`` are deliberately unconstrained audit-log snapshots,
    # not validated inputs.
    old_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    new_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    old_assignee_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    new_assignee_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
