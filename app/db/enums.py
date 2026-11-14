"""Shared enum types stored as ``str`` in PostgreSQL text columns."""

from enum import StrEnum

from sqlalchemy import Enum


def str_enum[StrEnumT: StrEnum](enum_cls: type[StrEnumT], length: int = 20) -> Enum:
    """Return a non-native ``Enum`` type: a ``VARCHAR`` column plus a CHECK.

    ``native_enum=False`` keeps a plain text column (no PostgreSQL ENUM type),
    and ``create_constraint=True`` emits the value-set CHECK constraint so the
    database enforces the enum members, matching the project's text-column
    convention. The constraint name comes from ``enum_cls.__name__.lower()``.
    """
    return Enum(
        enum_cls,
        native_enum=False,
        length=length,
        create_constraint=True,
        values_callable=lambda members: [member.value for member in members],
    )


class Role(StrEnum):
    support = "support"
    developer = "developer"
    admin = "admin"


class TicketStatus(StrEnum):
    new = "new"
    in_progress = "in_progress"
    waiting_info = "waiting_info"
    resolved = "resolved"
    rejected = "rejected"
    duplicate = "duplicate"


class Priority(StrEnum):
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"


class TicketEventType(StrEnum):
    created = "created"
    taken = "taken"
    status_changed = "status_changed"
    abandoned = "abandoned"
    reassigned = "reassigned"
    reopened = "reopened"


class DraftStep(StrEnum):
    description = "description"
    priority = "priority"
    request_id = "request_id"
    tenant_id = "tenant_id"
    confirm = "confirm"
