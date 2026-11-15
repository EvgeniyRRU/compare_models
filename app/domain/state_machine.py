"""Explicit ticket state machine: action definitions, transition legality, permissions.

Pure Python — no DB, no Telegram. Service layer enforces these; DB CHECK constraints
back up the data invariants independently.
"""

from dataclasses import dataclass
from enum import StrEnum

from app.db.models import ACTIVE_STATUSES, FINAL_STATUSES, EventType, Role, TicketStatus


class Action(StrEnum):
    take = "take"
    to_waiting = "to_waiting"
    to_progress = "to_progress"
    resolve = "resolve"
    reject = "reject"
    duplicate = "duplicate"
    abandon = "abandon"
    reopen = "reopen"
    reassign = "reassign"


NEW = TicketStatus.new
IN_PROGRESS = TicketStatus.in_progress
WAITING = TicketStatus.waiting_info

_STATUS_BY_ACTION: dict[Action, TicketStatus | None] = {
    Action.take: IN_PROGRESS,
    Action.to_waiting: WAITING,
    Action.to_progress: IN_PROGRESS,
    Action.resolve: TicketStatus.resolved,
    Action.reject: TicketStatus.rejected,
    Action.duplicate: TicketStatus.duplicate,
    Action.abandon: NEW,
    Action.reopen: NEW,
    Action.reassign: None,  # status unchanged
}

_EVENT_BY_ACTION: dict[Action, EventType] = {
    Action.take: EventType.taken,
    Action.to_waiting: EventType.status_changed,
    Action.to_progress: EventType.status_changed,
    Action.resolve: EventType.status_changed,
    Action.reject: EventType.status_changed,
    Action.duplicate: EventType.status_changed,
    Action.abandon: EventType.abandoned,
    Action.reopen: EventType.reopened,
    Action.reassign: EventType.reassigned,
}

_ALLOWED_FROM: dict[Action, frozenset[TicketStatus]] = {
    Action.take: frozenset({NEW}),
    Action.to_waiting: frozenset({IN_PROGRESS}),
    Action.to_progress: frozenset({WAITING}),
    Action.resolve: frozenset(ACTIVE_STATUSES),
    Action.reject: frozenset(ACTIVE_STATUSES),
    Action.duplicate: frozenset(ACTIVE_STATUSES),
    Action.abandon: frozenset(ACTIVE_STATUSES),
    Action.reopen: frozenset(FINAL_STATUSES),
    Action.reassign: frozenset(ACTIVE_STATUSES),
}

# Actions that only the current assignee or an admin may perform.
_ASSIGNEE_SCOPED = frozenset(
    {
        Action.to_waiting,
        Action.to_progress,
        Action.resolve,
        Action.reject,
        Action.duplicate,
        Action.abandon,
    }
)
_ADMIN_ONLY = frozenset({Action.reopen, Action.reassign})


@dataclass(frozen=True)
class ActionSpec:
    action: Action
    target_status: TicketStatus | None
    event_type: EventType
    allowed_from: frozenset[TicketStatus]


def spec(action: Action) -> ActionSpec:
    return ActionSpec(
        action=action,
        target_status=_STATUS_BY_ACTION[action],
        event_type=_EVENT_BY_ACTION[action],
        allowed_from=_ALLOWED_FROM[action],
    )


def is_transition_allowed(action: Action, current: TicketStatus) -> bool:
    return current in _ALLOWED_FROM[action]


def entering_new(action: Action) -> bool:
    """Actions that (re)open the ticket into status new — reminder (re)armed."""
    return _STATUS_BY_ACTION[action] is NEW


def leaving_new(action: Action) -> bool:
    return _ALLOWED_FROM[action] == frozenset({NEW})


def can_actor(
    action: Action, actor_role: Role | None, actor_id: int, assignee_id: int | None
) -> bool:
    """Permission check by *current DB role*. Admin has all support+developer rights."""
    if actor_role is None:
        return False
    if not actor_role.can_execute:
        return False  # support has no executor actions — ever
    if action in _ADMIN_ONLY:
        return actor_role is Role.admin
    if action is Action.take:
        return True
    if action in _ASSIGNEE_SCOPED:
        return actor_role is Role.admin or (assignee_id is not None and assignee_id == actor_id)
    return False


def available_buttons(status: TicketStatus) -> list[Action]:
    """Buttons on the dev-chat post for a ticket, in display order."""
    if status is NEW:
        return [Action.take]
    if status in ACTIVE_STATUSES:
        acts = [Action.to_progress] if status is WAITING else [Action.to_waiting]
        return [*acts, Action.resolve, Action.reject, Action.duplicate, Action.abandon]
    return []
