from enum import StrEnum


class Role(StrEnum):
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


FINAL_STATUSES: frozenset[TicketStatus] = frozenset(
    {TicketStatus.RESOLVED, TicketStatus.REJECTED, TicketStatus.DUPLICATE}
)
ACTIVE_STATUSES: frozenset[TicketStatus] = frozenset(
    {TicketStatus.IN_PROGRESS, TicketStatus.WAITING_INFO}
)


class Priority(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class TicketEventType(StrEnum):
    CREATED = "created"
    TAKEN = "taken"
    STATUS_CHANGED = "status_changed"
    RELEASED = "released"
    REASSIGNED = "reassigned"
    REOPENED = "reopened"


# События, после которых обращение входит в статус `new`: от них считается время до взятия.
NEW_ENTRY_EVENTS: frozenset[TicketEventType] = frozenset(
    {TicketEventType.CREATED, TicketEventType.RELEASED, TicketEventType.REOPENED}
)


class DraftStep(StrEnum):
    DESCRIPTION = "description"
    PRIORITY = "priority"
    REQUEST_ID = "request_id"
    TENANT_ID = "tenant_id"
    CONFIRM = "confirm"


TEXT_STEPS: frozenset[DraftStep] = frozenset(
    {DraftStep.DESCRIPTION, DraftStep.REQUEST_ID, DraftStep.TENANT_ID}
)

STATUS_LABELS: dict[TicketStatus, str] = {
    TicketStatus.NEW: "🆕 Новое",
    TicketStatus.IN_PROGRESS: "🛠 В работе",
    TicketStatus.WAITING_INFO: "⏳ Ожидает информации",
    TicketStatus.RESOLVED: "✅ Решено",
    TicketStatus.REJECTED: "🚫 Отклонено",
    TicketStatus.DUPLICATE: "♻️ Дубль",
}

PRIORITY_LABELS: dict[Priority, str] = {
    Priority.CRITICAL: "🔴 Критический",
    Priority.HIGH: "🟠 Высокий",
    Priority.MEDIUM: "🟡 Средний",
    Priority.LOW: "🟢 Низкий",
}

ROLE_LABELS: dict[Role, str] = {
    Role.SUPPORT: "Поддержка",
    Role.DEVELOPER: "Разработчик",
    Role.ADMIN: "Администратор",
}
