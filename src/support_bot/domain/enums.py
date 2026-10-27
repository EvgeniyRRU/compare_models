from enum import StrEnum


class Role(StrEnum):
    SUPPORT = "support"
    DEVELOPER = "developer"
    ADMIN = "admin"


class Status(StrEnum):
    NEW = "new"
    IN_PROGRESS = "in_progress"
    WAITING_INFO = "waiting_info"
    RESOLVED = "resolved"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"


class Priority(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class DraftStep(StrEnum):
    DESCRIPTION = "description"
    PRIORITY = "priority"
    REQUEST_ID = "request_id"
    TENANT_ID = "tenant_id"
    CONFIRM = "confirm"


ACTIVE = frozenset({Status.IN_PROGRESS, Status.WAITING_INFO})
FINAL = frozenset({Status.RESOLVED, Status.REJECTED, Status.DUPLICATE})
EXECUTOR_ROLES = frozenset({Role.DEVELOPER, Role.ADMIN})
