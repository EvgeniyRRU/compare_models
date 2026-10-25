"""ORM-модели. `updated_at` во всех таблицах обновляет триггер БД `set_updated_at()`."""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    FetchedValue,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from support_bot.domain.enums import DraftStep, Priority, Role, TicketEventType, TicketStatus
from support_bot.domain.validation import (
    DESCRIPTION_MAX_LEN,
    REQUEST_ID_MAX_LEN,
    TENANT_ID_MAX_LEN,
)

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def pg_enum[E: StrEnum](enum_cls: type[E], name: str) -> SAEnum:
    return SAEnum(
        enum_cls,
        name=name,
        values_callable=lambda members: [m.value for m in members],
        validate_strings=True,
    )


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[Any, Any]] = {datetime: DateTime(timezone=True)}


def created_at_column() -> Mapped[datetime]:
    return mapped_column(server_default=func.now())


def updated_at_column() -> Mapped[datetime]:
    return mapped_column(server_default=func.now(), server_onupdate=FetchedValue())


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(256))
    last_name: Mapped[str | None] = mapped_column(String(256))
    role: Mapped[Role | None] = mapped_column(pg_enum(Role, "user_role"))
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


_FINAL = "status IN ('resolved', 'rejected', 'duplicate')"


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        CheckConstraint(
            "status <> 'new' OR (assignee_id IS NULL AND taken_at IS NULL)", name="new_unassigned"
        ),
        CheckConstraint(
            "status NOT IN ('in_progress', 'waiting_info') "
            "OR (assignee_id IS NOT NULL AND taken_at IS NOT NULL)",
            name="active_assigned",
        ),
        CheckConstraint(f"({_FINAL}) = (closed_at IS NOT NULL)", name="closed_at_iff_final"),
        CheckConstraint("status = 'new' OR next_reminder_at IS NULL", name="reminder_only_new"),
        CheckConstraint("length(btrim(description)) > 0", name="description_not_empty"),
        CheckConstraint("length(btrim(request_id)) > 0", name="request_id_not_empty"),
        CheckConstraint("version >= 1", name="version_positive"),
        Index(
            "ix_tickets_due_reminders",
            "next_reminder_at",
            postgresql_where=text("status = 'new'"),
        ),
        Index(
            "ix_tickets_post_sync_pending",
            "id",
            postgresql_where=text("message_id IS NULL OR post_synced_version < version"),
        ),
        Index("ix_tickets_created_at", text("created_at DESC"), text("id DESC")),
        Index("ix_tickets_author_created", "author_id", text("created_at DESC")),
        Index("ix_tickets_assignee_created", "assignee_id", text("created_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    author_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    assignee_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))
    description: Mapped[str] = mapped_column(String(DESCRIPTION_MAX_LEN))
    priority: Mapped[Priority] = mapped_column(pg_enum(Priority, "ticket_priority"))
    request_id: Mapped[str] = mapped_column(String(REQUEST_ID_MAX_LEN))
    tenant_id: Mapped[str | None] = mapped_column(String(TENANT_ID_MAX_LEN))
    status: Mapped[TicketStatus] = mapped_column(
        pg_enum(TicketStatus, "ticket_status"), server_default=TicketStatus.NEW.value
    )
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()
    taken_at: Mapped[datetime | None]
    closed_at: Mapped[datetime | None]
    next_reminder_at: Mapped[datetime | None]
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    post_synced_version: Mapped[int] = mapped_column(Integer, server_default=text("0"))


class TicketEvent(Base):
    __tablename__ = "ticket_events"
    __table_args__ = (Index("ix_ticket_events_ticket_id_id", "ticket_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    ticket_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("tickets.id"))
    event_type: Mapped[TicketEventType] = mapped_column(
        pg_enum(TicketEventType, "ticket_event_type")
    )
    actor_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    old_status: Mapped[TicketStatus | None] = mapped_column(pg_enum(TicketStatus, "ticket_status"))
    new_status: Mapped[TicketStatus] = mapped_column(pg_enum(TicketStatus, "ticket_status"))
    old_assignee_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))
    new_assignee_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))
    version_after: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = created_at_column()


class TicketDraft(Base):
    __tablename__ = "ticket_drafts"
    __table_args__ = (CheckConstraint("version >= 1", name="version_positive"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), unique=True)
    step: Mapped[DraftStep] = mapped_column(pg_enum(DraftStep, "draft_step"))
    description: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[Priority | None] = mapped_column(pg_enum(Priority, "ticket_priority"))
    request_id: Mapped[str | None] = mapped_column(Text)
    tenant_id: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    prompt_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class ProcessedUpdate(Base):
    __tablename__ = "processed_updates"
    __table_args__ = (Index("ix_processed_updates_processed_at", "processed_at"),)

    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    processed_at: Mapped[datetime] = mapped_column(server_default=func.now())


TABLES_WITH_UPDATED_AT = ("users", "tickets", "ticket_drafts")
