"""Явная state machine статусов обращения (SPEC «Сущности и статусы»)."""

from __future__ import annotations

from dataclasses import dataclass

from app.services.errors import PermissionDenied

PRIORITIES = ("critical", "high", "medium", "low")
STATUSES = ("new", "in_progress", "waiting_info", "resolved", "rejected", "duplicate")
FINAL_STATUSES = ("resolved", "rejected", "duplicate")
EXECUTOR_ROLES = ("developer", "admin")
CREATE_ROLES = ("support", "admin")


@dataclass(frozen=True)
class Transition:
    action: str
    from_statuses: frozenset[str]
    to_status: str | None  # None — статус не меняется (reassign)
    roles: frozenset[str]  # разрешённые роли актора
    assignee_or_admin: bool  # актор — исполнитель или admin
    event_type: str
    clears_assignee: bool = False
    sets_taken_at: bool = False
    clears_taken_at: bool = False
    sets_closed_at: bool = False
    clears_closed_at: bool = False
    sets_reminder: bool = False
    clears_reminder: bool = False


TRANSITIONS: dict[str, Transition] = {
    "take": Transition(
        "take",
        frozenset({"new"}),
        "in_progress",
        frozenset(EXECUTOR_ROLES),
        False,
        "taken",
        sets_taken_at=True,
        clears_reminder=True,
    ),
    "to_waiting": Transition(
        "to_waiting",
        frozenset({"in_progress"}),
        "waiting_info",
        frozenset(EXECUTOR_ROLES),
        True,
        "status_changed",
    ),
    "to_in_progress": Transition(
        "to_in_progress",
        frozenset({"waiting_info"}),
        "in_progress",
        frozenset(EXECUTOR_ROLES),
        True,
        "status_changed",
    ),
    "to_resolved": Transition(
        "to_resolved",
        frozenset({"in_progress", "waiting_info"}),
        "resolved",
        frozenset(EXECUTOR_ROLES),
        True,
        "status_changed",
        sets_closed_at=True,
    ),
    "to_rejected": Transition(
        "to_rejected",
        frozenset({"in_progress", "waiting_info"}),
        "rejected",
        frozenset(EXECUTOR_ROLES),
        True,
        "status_changed",
        sets_closed_at=True,
    ),
    "to_duplicate": Transition(
        "to_duplicate",
        frozenset({"in_progress", "waiting_info"}),
        "duplicate",
        frozenset(EXECUTOR_ROLES),
        True,
        "status_changed",
        sets_closed_at=True,
    ),
    "decline": Transition(
        "decline",
        frozenset({"in_progress", "waiting_info"}),
        "new",
        frozenset(EXECUTOR_ROLES),
        True,
        "declined",
        clears_assignee=True,
        clears_taken_at=True,
        sets_reminder=True,
    ),
    "reopen": Transition(
        "reopen",
        frozenset(FINAL_STATUSES),
        "new",
        frozenset({"admin"}),
        False,
        "reopened",
        clears_assignee=True,
        clears_taken_at=True,
        clears_closed_at=True,
        sets_reminder=True,
    ),
    "reassign": Transition(
        "reassign",
        frozenset({"in_progress", "waiting_info"}),
        None,
        frozenset({"admin"}),
        False,
        "reassigned",
        sets_taken_at=True,
    ),
}


def check_permission(
    role: str | None, actor_id: int, assignee_id: int | None, t: Transition
) -> None:
    if role is None or role not in t.roles:
        raise PermissionDenied("Недостаточно прав для этого действия.")
    if t.assignee_or_admin and role != "admin" and actor_id != assignee_id:
        raise PermissionDenied("Это действие доступно исполнителю обращения или администратору.")
