from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from support_bot.db.enums import (
    ASSIGNED_STATUSES,
    FINAL_STATUSES,
    TicketStatus,
    UserRole,
)
from support_bot.domain.errors import PermissionDenied, StatusConflict


class TicketAction(StrEnum):
    TAKE = "take"
    WAIT_INFO = "wait_info"
    TO_WORK = "to_work"
    RESOLVE = "resolve"
    REJECT = "reject"
    DUPLICATE = "duplicate"
    DECLINE = "decline"
    REOPEN = "reopen"
    REASSIGN = "reassign"


@dataclass(frozen=True, slots=True)
class TransitionRule:
    allowed_from: frozenset[TicketStatus]
    to_status: TicketStatus | None
    admin_only: bool = False
    take_right: bool = False
    keeps_assignee: bool = True


RULES: dict[TicketAction, TransitionRule] = {
    TicketAction.TAKE: TransitionRule(
        allowed_from=frozenset({TicketStatus.NEW}),
        to_status=TicketStatus.IN_PROGRESS,
        take_right=True,
        keeps_assignee=False,
    ),
    TicketAction.WAIT_INFO: TransitionRule(
        allowed_from=frozenset({TicketStatus.IN_PROGRESS}),
        to_status=TicketStatus.WAITING_INFO,
    ),
    TicketAction.TO_WORK: TransitionRule(
        allowed_from=frozenset({TicketStatus.WAITING_INFO}),
        to_status=TicketStatus.IN_PROGRESS,
    ),
    TicketAction.RESOLVE: TransitionRule(
        allowed_from=frozenset(ASSIGNED_STATUSES),
        to_status=TicketStatus.RESOLVED,
    ),
    TicketAction.REJECT: TransitionRule(
        allowed_from=frozenset(ASSIGNED_STATUSES),
        to_status=TicketStatus.REJECTED,
    ),
    TicketAction.DUPLICATE: TransitionRule(
        allowed_from=frozenset(ASSIGNED_STATUSES),
        to_status=TicketStatus.DUPLICATE,
    ),
    TicketAction.DECLINE: TransitionRule(
        allowed_from=frozenset(ASSIGNED_STATUSES),
        to_status=TicketStatus.NEW,
        keeps_assignee=False,
    ),
    TicketAction.REOPEN: TransitionRule(
        allowed_from=FINAL_STATUSES,
        to_status=TicketStatus.NEW,
        admin_only=True,
        keeps_assignee=False,
    ),
    TicketAction.REASSIGN: TransitionRule(
        allowed_from=frozenset(ASSIGNED_STATUSES),
        to_status=None,
        admin_only=True,
    ),
}


def rule_for(status: TicketStatus, action: TicketAction) -> TransitionRule:
    rule = RULES[action]
    if status not in rule.allowed_from:
        raise StatusConflict(f"action {action.value} is not allowed from {status.value}")
    return rule


def check_permission(
    action: TicketAction,
    rule: TransitionRule,
    role: UserRole | None,
    user_id: int,
    assignee_id: int | None,
) -> None:
    if role is None:
        raise PermissionDenied("user has no role")
    if rule.admin_only:
        if role != UserRole.ADMIN:
            raise PermissionDenied("admin role required")
        return
    if rule.take_right:
        if role not in (UserRole.DEVELOPER, UserRole.ADMIN):
            raise PermissionDenied("developer role required")
        return
    if role == UserRole.ADMIN:
        return
    if role != UserRole.DEVELOPER:
        raise PermissionDenied("developer or admin role required")
    if assignee_id != user_id:
        raise PermissionDenied("only the assignee or an admin can do this")
