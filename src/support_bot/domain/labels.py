from __future__ import annotations

from support_bot.db.enums import TicketPriority, TicketStatus, UserRole

PRIORITY_LABELS: dict[TicketPriority, str] = {
    TicketPriority.CRITICAL: "Критический",
    TicketPriority.HIGH: "Высокий",
    TicketPriority.MEDIUM: "Средний",
    TicketPriority.LOW: "Низкий",
}

STATUS_LABELS: dict[TicketStatus, str] = {
    TicketStatus.NEW: "Новое",
    TicketStatus.IN_PROGRESS: "В работе",
    TicketStatus.WAITING_INFO: "Ожидает информации",
    TicketStatus.RESOLVED: "Решено",
    TicketStatus.REJECTED: "Отклонено",
    TicketStatus.DUPLICATE: "Дубль",
}

ROLE_LABELS: dict[UserRole, str] = {
    UserRole.SUPPORT: "Поддержка",
    UserRole.DEVELOPER: "Разработчик",
    UserRole.ADMIN: "Администратор",
}
