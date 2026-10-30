"""Чистая state machine обращений: без БД, Telegram и HTTP."""

from dataclasses import dataclass
from enum import StrEnum


class Role(StrEnum):
    SUPPORT = "support"
    DEVELOPER = "developer"
    ADMIN = "admin"


ASSIGNEE_ROLES = frozenset({Role.DEVELOPER, Role.ADMIN})


class Status(StrEnum):
    NEW = "new"
    IN_PROGRESS = "in_progress"
    WAITING_INFO = "waiting_info"
    RESOLVED = "resolved"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"


FINAL_STATUSES = frozenset({Status.RESOLVED, Status.REJECTED, Status.DUPLICATE})
ACTIVE_STATUSES = frozenset({Status.IN_PROGRESS, Status.WAITING_INFO})


class Priority(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Action(StrEnum):
    TAKE = "take"
    WAIT = "wait"  # in_progress -> waiting_info
    RESUME = "resume"  # waiting_info -> in_progress
    RESOLVE = "resolve"
    REJECT = "reject"
    DUPLICATE = "duplicate"
    RELEASE = "release"  # «Отказаться»: -> new
    REOPEN = "reopen"
    REASSIGN = "reassign"


class EventType(StrEnum):
    CREATED = "created"
    TAKEN = "taken"
    STATUS_CHANGED = "status_changed"
    RELEASED = "released"
    REASSIGNED = "reassigned"
    REOPENED = "reopened"


class TicketError(Exception):
    """Базовая ошибка бизнес-правил; message показывается пользователю."""


class PermissionDenied(TicketError):
    pass


class InvalidTransition(TicketError):
    pass


class StaleTicket(TicketError):
    """Устаревшая version/status: «Обращение изменилось»."""


class AlreadyTaken(TicketError):
    """«Уже взято»."""


class NotFound(TicketError):
    pass


@dataclass(frozen=True)
class Actor:
    id: int
    role: Role | None


@dataclass(frozen=True)
class Transition:
    new_status: Status
    event_type: EventType
    assignee: str  # "set" (актор или целевой) | "keep" | "clear"
    taken_at: str  # "set" | "keep" | "clear"
    closed_at: str  # "set" | "keep" | "clear"
    reminder: str  # "schedule" | "clear" | "keep"


_FINAL_ACTIONS = {
    Action.RESOLVE: Status.RESOLVED,
    Action.REJECT: Status.REJECTED,
    Action.DUPLICATE: Status.DUPLICATE,
}


def can_be_assignee(role: Role | None) -> bool:
    return role in ASSIGNEE_ROLES


def plan_transition(
    status: Status,
    assignee_id: int | None,
    action: Action,
    actor: Actor,
    target_assignee_id: int | None = None,
) -> Transition:
    """Проверяет право и допустимость действия, возвращает описание изменения."""
    is_admin = actor.role == Role.ADMIN
    is_assignee = assignee_id is not None and assignee_id == actor.id and can_be_assignee(actor.role)

    if action == Action.TAKE:
        if not can_be_assignee(actor.role):
            raise PermissionDenied("Брать обращения могут разработчики и админы")
        if status != Status.NEW:
            raise AlreadyTaken("Уже взято")
        return Transition(Status.IN_PROGRESS, EventType.TAKEN, "set", "set", "keep", "clear")

    if action == Action.REOPEN:
        if not is_admin:
            raise PermissionDenied("Переоткрывать обращения может только админ")
        if status not in FINAL_STATUSES:
            raise InvalidTransition("Переоткрыть можно только закрытое обращение")
        return Transition(Status.NEW, EventType.REOPENED, "clear", "clear", "clear", "schedule")

    if action == Action.REASSIGN:
        if not is_admin:
            raise PermissionDenied("Переназначать обращения может только админ")
        if status not in ACTIVE_STATUSES:
            raise InvalidTransition("Переназначить можно только взятое обращение")
        if target_assignee_id is None:
            raise InvalidTransition("Не указан новый исполнитель")
        if target_assignee_id == assignee_id:
            raise InvalidTransition("Обращение уже назначено на этого пользователя")
        return Transition(status, EventType.REASSIGNED, "set", "set", "keep", "keep")

    # остальные действия: исполнитель или админ, только для взятого обращения
    if not (is_admin or is_assignee):
        raise PermissionDenied("Действие доступно исполнителю или админу")
    if status not in ACTIVE_STATUSES:
        raise InvalidTransition("Недопустимый переход статуса")

    if action == Action.WAIT:
        if status != Status.IN_PROGRESS:
            raise InvalidTransition("Недопустимый переход статуса")
        return Transition(Status.WAITING_INFO, EventType.STATUS_CHANGED, "keep", "keep", "keep", "keep")
    if action == Action.RESUME:
        if status != Status.WAITING_INFO:
            raise InvalidTransition("Недопустимый переход статуса")
        return Transition(Status.IN_PROGRESS, EventType.STATUS_CHANGED, "keep", "keep", "keep", "keep")
    if action in _FINAL_ACTIONS:
        return Transition(
            _FINAL_ACTIONS[action], EventType.STATUS_CHANGED, "keep", "keep", "set", "keep"
        )
    if action == Action.RELEASE:
        return Transition(Status.NEW, EventType.RELEASED, "clear", "clear", "keep", "schedule")
    raise InvalidTransition("Неизвестное действие")  # pragma: no cover
