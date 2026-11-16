from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def created_at_column() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def updated_at_column() -> Mapped[datetime]:
    # ЕДИНЫЙ механизм updated_at для всех таблиц (решение по SPEC): onupdate в SQLAlchemy.
    # Покрывает все ORM и Core-обновления приложения; сырой SQL намеренно не покрывается.
    return mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
