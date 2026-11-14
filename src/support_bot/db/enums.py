from __future__ import annotations

from enum import StrEnum

from sqlalchemy import Enum as SAEnum


class UserRole(StrEnum):
    SUPPORT = "support"
    DEVELOPER = "developer"
    ADMIN = "admin"


class TicketStatus(StrEnum):
    NEW = "new"
    IN_PROGRESS = "in_progress"
    WAITING_INFO = "waiting_info"
    RESOLVED = "resolved"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"


class TicketPriority(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class TicketEventType(StrEnum):
    CREATED = "created"
    TAKEN = "taken"
    STATUS_CHANGED = "status_changed"
    DECLINED = "declined"
    REASSIGNED = "reassigned"
    REOPENED = "reopened"


class DraftStep(StrEnum):
    DESCRIPTION = "description"
    PRIORITY = "priority"
    REQUEST_ID = "request_id"
    TENANT_ID = "tenant_id"
    CONFIRM = "confirm"


def _values(enum_cls: type[StrEnum]) -> list[str]:
    return [member.value for member in enum_cls]


USER_ROLE = SAEnum(
    UserRole,
    name="user_role",
    native_enum=True,
    values_callable=_values,
)
TICKET_STATUS = SAEnum(
    TicketStatus,
    name="ticket_status",
    native_enum=True,
    values_callable=_values,
)
TICKET_PRIORITY = SAEnum(
    TicketPriority,
    name="ticket_priority",
    native_enum=True,
    values_callable=_values,
)
TICKET_EVENT_TYPE = SAEnum(
    TicketEventType,
    name="ticket_event_type",
    native_enum=True,
    values_callable=_values,
)
DRAFT_STEP = SAEnum(
    DraftStep,
    name="draft_step",
    native_enum=True,
    values_callable=_values,
)

FINAL_STATUSES: frozenset[TicketStatus] = frozenset(
    {TicketStatus.RESOLVED, TicketStatus.REJECTED, TicketStatus.DUPLICATE}
)
OPEN_STATUSES: frozenset[TicketStatus] = frozenset(
    {TicketStatus.NEW, TicketStatus.IN_PROGRESS, TicketStatus.WAITING_INFO}
)
ASSIGNED_STATUSES: frozenset[TicketStatus] = frozenset(
    {TicketStatus.IN_PROGRESS, TicketStatus.WAITING_INFO}
)
