from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from support_bot.db.base import Base
from support_bot.db.enums import DRAFT_STEP, TICKET_PRIORITY, DraftStep, TicketPriority


class TicketDraft(Base):
    __tablename__ = "ticket_drafts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    step: Mapped[DraftStep] = mapped_column(
        DRAFT_STEP, nullable=False, server_default="description"
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[TicketPriority | None] = mapped_column(TICKET_PRIORITY, nullable=True)
    request_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    tenant_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1", default=1)
    prompt_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        index=True,
    )
