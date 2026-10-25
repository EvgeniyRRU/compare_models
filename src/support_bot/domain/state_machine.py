"""Явная state machine обращения и правила прав. Чистая логика без I/O."""

from dataclasses import dataclass
from enum import Enum, StrEnum

from support_bot.domain.enums import (
    ACTIVE_STATUSES,
    FINAL_STATUSES,
    Role,
    TicketEventType,
    TicketStatus,
)
from support_bot.domain.errors import (
    AccessDeniedError,
    InvalidAssigneeError,
    InvalidTransitionError,
    PermissionDeniedError,
)


class Action(StrEnum):
    TAKE = "take"
    TO_WAITING = "to_waiting"
    TO_WORK = "to_work"
    RESOLVE = "resolve"
    REJECT = "reject"
    DUPLICATE = "duplicate"
    RELEASE = "release"
    REOPEN = "reopen"


class Permission(Enum):
    TAKER = "taker"  # developer или admin
    ASSIGNEE_OR_ADMIN = "assignee_or_admin"
    ADMIN = "admin"


class TimeOp(Enum):
    KEEP = "keep"
    NOW = "now"
    CLEAR = "clear"


class ReminderOp(Enum):
    KEEP = "keep"
    SCHEDULE = "schedule"
    CLEAR = "clear"


@dataclass(frozen=True)
class Transition:
    action: Action
    sources: frozenset[TicketStatus]
    target: TicketStatus
    permission: Permission
    event_type: TicketEventType


def _t(
    action: Action,
    sources: set[TicketStatus] | frozenset[TicketStatus],
    target: TicketStatus,
    permission: Permission,
    event_type: TicketEventType,
) -> Transition:
    return Transition(action, frozenset(sources), target, permission, event_type)


_A = Permission.ASSIGNEE_OR_ADMIN
_CHANGED = TicketEventType.STATUS_CHANGED

TRANSITIONS: dict[Action, Transition] = {
    Action.TAKE: _t(
        Action.TAKE,
        {TicketStatus.NEW},
        TicketStatus.IN_PROGRESS,
        Permission.TAKER,
        TicketEventType.TAKEN,
    ),
    Action.TO_WAITING: _t(
        Action.TO_WAITING, {TicketStatus.IN_PROGRESS}, TicketStatus.WAITING_INFO, _A, _CHANGED
    ),
    Action.TO_WORK: _t(
        Action.TO_WORK, {TicketStatus.WAITING_INFO}, TicketStatus.IN_PROGRESS, _A, _CHANGED
    ),
    Action.RESOLVE: _t(Action.RESOLVE, ACTIVE_STATUSES, TicketStatus.RESOLVED, _A, _CHANGED),
    Action.REJECT: _t(Action.REJECT, ACTIVE_STATUSES, TicketStatus.REJECTED, _A, _CHANGED),
    Action.DUPLICATE: _t(Action.DUPLICATE, ACTIVE_STATUSES, TicketStatus.DUPLICATE, _A, _CHANGED),
    Action.RELEASE: _t(
        Action.RELEASE, ACTIVE_STATUSES, TicketStatus.NEW, _A, TicketEventType.RELEASED
    ),
    Action.REOPEN: _t(
        Action.REOPEN, FINAL_STATUSES, TicketStatus.NEW, Permission.ADMIN, TicketEventType.REOPENED
    ),
}

# Кнопки поста в чате разработчиков, по статусам, в порядке отображения.
POST_ACTIONS: dict[TicketStatus, tuple[Action, ...]] = {
    TicketStatus.NEW: (Action.TAKE,),
    TicketStatus.IN_PROGRESS: (
        Action.TO_WAITING,
        Action.RESOLVE,
        Action.REJECT,
        Action.DUPLICATE,
        Action.RELEASE,
    ),
    TicketStatus.WAITING_INFO: (
        Action.TO_WORK,
        Action.RESOLVE,
        Action.REJECT,
        Action.DUPLICATE,
        Action.RELEASE,
    ),
    TicketStatus.RESOLVED: (),
    TicketStatus.REJECTED: (),
    TicketStatus.DUPLICATE: (),
}

# Действия, которые можно выполнить кнопкой поста (переоткрытие — только в Mini App).
BUTTON_ACTIONS: frozenset[Action] = frozenset(TRANSITIONS) - {Action.REOPEN}


@dataclass(frozen=True)
class Changes:
    """Новые значения полей обращения для одного перехода (одно увеличение version)."""

    status: TicketStatus
    assignee_changed: bool
    assignee_id: int | None
    taken_at: TimeOp
    closed_at: TimeOp
    reminder: ReminderOp


def require_role(role: Role | None) -> Role:
    if role is None:
        raise AccessDeniedError()
    return role


def can_create(role: Role | None) -> bool:
    return role in (Role.SUPPORT, Role.ADMIN)


def can_be_assignee(role: Role | None) -> bool:
    return role in (Role.DEVELOPER, Role.ADMIN)


def is_admin(role: Role | None) -> bool:
    return role is Role.ADMIN


def get_transition(action: Action, status: TicketStatus) -> Transition:
    transition = TRANSITIONS[action]
    if status not in transition.sources:
        raise InvalidTransitionError()
    return transition


def check_permission(
    transition: Transition,
    actor_id: int,
    actor_role: Role | None,
    assignee_id: int | None,
) -> None:
    role = require_role(actor_role)
    match transition.permission:
        case Permission.TAKER:
            allowed = can_be_assignee(role)
        case Permission.ASSIGNEE_OR_ADMIN:
            allowed = is_admin(role) or (assignee_id is not None and assignee_id == actor_id)
        case Permission.ADMIN:
            allowed = is_admin(role)
    if not allowed:
        raise PermissionDeniedError()


def plan_transition(transition: Transition, actor_id: int) -> Changes:
    target = transition.target
    match transition.action:
        case Action.TAKE:
            return Changes(target, True, actor_id, TimeOp.NOW, TimeOp.KEEP, ReminderOp.CLEAR)
        case Action.RELEASE:
            return Changes(target, True, None, TimeOp.CLEAR, TimeOp.KEEP, ReminderOp.SCHEDULE)
        case Action.REOPEN:
            return Changes(target, True, None, TimeOp.CLEAR, TimeOp.CLEAR, ReminderOp.SCHEDULE)
        case Action.RESOLVE | Action.REJECT | Action.DUPLICATE:
            return Changes(target, False, None, TimeOp.KEEP, TimeOp.NOW, ReminderOp.KEEP)
        case Action.TO_WAITING | Action.TO_WORK:
            return Changes(target, False, None, TimeOp.KEEP, TimeOp.KEEP, ReminderOp.KEEP)


def check_reassign(
    status: TicketStatus,
    actor_role: Role | None,
    current_assignee_id: int | None,
    new_assignee_id: int,
    new_assignee_role: Role | None,
) -> Changes:
    """Переназначение: только admin, только во взятом обращении, только на исполнителя."""
    if not is_admin(require_role(actor_role)):
        raise PermissionDeniedError()
    if status not in ACTIVE_STATUSES:
        raise InvalidTransitionError()
    if not can_be_assignee(new_assignee_role):
        raise InvalidAssigneeError()
    if current_assignee_id == new_assignee_id:
        raise InvalidAssigneeError("Пользователь уже является исполнителем этого обращения")
    return Changes(status, True, new_assignee_id, TimeOp.NOW, TimeOp.KEEP, ReminderOp.KEEP)
