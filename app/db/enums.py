"""Shared enum types stored as ``str`` in PostgreSQL text columns."""

from enum import StrEnum


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
