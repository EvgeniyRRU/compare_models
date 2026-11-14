from __future__ import annotations

import pytest

from support_bot.db.enums import TicketStatus, UserRole
from support_bot.domain.errors import PermissionDenied, StatusConflict
from support_bot.domain.state_machine import (
    RULES,
    TicketAction,
    check_permission,
    rule_for,
)

ALLOWED = [
    (TicketStatus.NEW, TicketAction.TAKE),
    (TicketStatus.IN_PROGRESS, TicketAction.WAIT_INFO),
    (TicketStatus.WAITING_INFO, TicketAction.TO_WORK),
    (TicketStatus.IN_PROGRESS, TicketAction.RESOLVE),
    (TicketStatus.WAITING_INFO, TicketAction.REJECT),
    (TicketStatus.IN_PROGRESS, TicketAction.DUPLICATE),
    (TicketStatus.IN_PROGRESS, TicketAction.DECLINE),
    (TicketStatus.WAITING_INFO, TicketAction.DECLINE),
    (TicketStatus.RESOLVED, TicketAction.REOPEN),
    (TicketStatus.REJECTED, TicketAction.REOPEN),
    (TicketStatus.DUPLICATE, TicketAction.REOPEN),
    (TicketStatus.IN_PROGRESS, TicketAction.REASSIGN),
    (TicketStatus.WAITING_INFO, TicketAction.REASSIGN),
]

FORBIDDEN = [
    (TicketStatus.NEW, TicketAction.RESOLVE),
    (TicketStatus.NEW, TicketAction.REJECT),
    (TicketStatus.NEW, TicketAction.DUPLICATE),
    (TicketStatus.NEW, TicketAction.WAIT_INFO),
    (TicketStatus.NEW, TicketAction.REOPEN),
    (TicketStatus.RESOLVED, TicketAction.TAKE),
    (TicketStatus.RESOLVED, TicketAction.WAIT_INFO),
    (TicketStatus.IN_PROGRESS, TicketAction.REOPEN),
]


@pytest.mark.parametrize(("status", "action"), ALLOWED)
def test_allowed_transitions(status: TicketStatus, action: TicketAction) -> None:
    assert status in rule_for(status, action).allowed_from


@pytest.mark.parametrize(("status", "action"), FORBIDDEN)
def test_forbidden_transitions(status: TicketStatus, action: TicketAction) -> None:
    with pytest.raises(StatusConflict):
        rule_for(status, action)


def test_take_requires_developer_or_admin() -> None:
    rule = RULES[TicketAction.TAKE]
    check_permission(TicketAction.TAKE, rule, UserRole.DEVELOPER, 1, None)
    check_permission(TicketAction.TAKE, rule, UserRole.ADMIN, 1, None)
    with pytest.raises(PermissionDenied):
        check_permission(TicketAction.TAKE, rule, UserRole.SUPPORT, 1, None)
    with pytest.raises(PermissionDenied):
        check_permission(TicketAction.TAKE, rule, None, 1, None)


def test_only_assignee_or_admin_can_change_taken_ticket() -> None:
    rule = RULES[TicketAction.RESOLVE]
    check_permission(TicketAction.RESOLVE, rule, UserRole.DEVELOPER, 5, 5)
    check_permission(TicketAction.RESOLVE, rule, UserRole.ADMIN, 9, 5)
    with pytest.raises(PermissionDenied):
        check_permission(TicketAction.RESOLVE, rule, UserRole.DEVELOPER, 9, 5)
    with pytest.raises(PermissionDenied):
        check_permission(TicketAction.RESOLVE, rule, UserRole.SUPPORT, 5, 5)


@pytest.mark.parametrize("action", [TicketAction.REOPEN, TicketAction.REASSIGN])
def test_admin_only_actions(action: TicketAction) -> None:
    rule = RULES[action]
    check_permission(action, rule, UserRole.ADMIN, 1, 1)
    with pytest.raises(PermissionDenied):
        check_permission(action, rule, UserRole.DEVELOPER, 1, 1)
