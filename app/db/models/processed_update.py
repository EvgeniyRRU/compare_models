"""``processed_updates`` table: Telegram update deduplication ledger."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ProcessedUpdate(Base):
    __tablename__ = "processed_updates"

    # ``update_id`` is Telegram's own monotonic id, not a database identity.
    # ``autoincrement=False`` keeps it a plain bigint (no SERIAL/sequence):
    # the application always supplies the value.
    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
