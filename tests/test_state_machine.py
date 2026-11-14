"""Tests for the pure ticket state machine (no database)."""

import pytest

from app.db.enums import Role, TicketStatus
from app.services.state_machine import (
    TicketAction,
    can_transition,
    is_action_allowed,
    transition,
)

TRANSITION_CASES: list[tuple[TicketStatus, TicketAction, TicketStatus]] = [
    (TicketStatus.new, TicketAction.take, TicketStatus.in_progress),
    (TicketStatus.in_progress, TicketAction.to_waiting, TicketStatus.waiting_info),
    (TicketStatus.waiting_info, TicketAction.to_progress, TicketStatus.in_progress),
    (TicketStatus.in_progress, TicketAction.resolve, TicketStatus.resolved),
    (TicketStatus.in_progress, TicketAction.reject, TicketStatus.rejected),
    (TicketStatus.in_progress, TicketAction.duplicate, TicketStatus.duplicate),
    (TicketStatus.waiting_info, TicketAction.resolve, TicketStatus.resolved),
    (TicketStatus.waiting_info, TicketAction.reject, TicketStatus.rejected),
    (TicketStatus.waiting_info, TicketAction.duplicate, TicketStatus.duplicate),
    (TicketStatus.in_progress, TicketAction.abandon, TicketStatus.new),
    (TicketStatus.waiting_info, TicketAction.abandon, TicketStatus.new),
    (TicketStatus.resolved, TicketAction.reopen, TicketStatus.new),
    (TicketStatus.rejected, TicketAction.reopen, TicketStatus.new),
    (TicketStatus.duplicate, TicketAction.reopen, TicketStatus.new),
    (TicketStatus.in_progress, TicketAction.reassign, TicketStatus.in_progress),
    (TicketStatus.waiting_info, TicketAction.reassign, TicketStatus.waiting_info),
]

ALLOWED_PAIRS = {(from_status, action) for from_status, action, _ in TRANSITION_CASES}


@pytest.mark.parametrize(("from_status", "action", "to"), TRANSITION_CASES)
def test_table_transition(
    from_status: TicketStatus, action: TicketAction, to: TicketStatus
) -> None:
    assert can_transition(from_status, action)
    assert transition(from_status, action) is to


def test_no_transitions_outside_table() -> None:
    for from_status in TicketStatus:
        for action in TicketAction:
            assert can_transition(from_status, action) is ((from_status, action) in ALLOWED_PAIRS)


def test_transition_raises_for_unknown_action() -> None:
    with pytest.raises(ValueError):
        transition(TicketStatus.new, TicketAction.resolve)


@pytest.mark.parametrize(
    "action",
    [TicketAction.resolve, TicketAction.reject, TicketAction.duplicate],
)
def test_new_to_final_forbidden(action: TicketAction) -> None:
    assert not can_transition(TicketStatus.new, action)
    with pytest.raises(ValueError):
        transition(TicketStatus.new, action)


@pytest.mark.parametrize(
    "from_status",
    [TicketStatus.new, TicketStatus.in_progress, TicketStatus.waiting_info],
)
def test_reopen_from_non_final_forbidden(from_status: TicketStatus) -> None:
    assert not can_transition(from_status, TicketAction.reopen)


@pytest.mark.parametrize(
    "from_status",
    [
        TicketStatus.new,
        TicketStatus.resolved,
        TicketStatus.rejected,
        TicketStatus.duplicate,
    ],
)
def test_reassign_from_invalid_forbidden(from_status: TicketStatus) -> None:
    assert not can_transition(from_status, TicketAction.reassign)


# --- permissions ---


def test_developer_can_take_from_new() -> None:
    assert is_action_allowed(TicketAction.take, TicketStatus.new, Role.developer, False)


def test_admin_can_take_from_new() -> None:
    assert is_action_allowed(TicketAction.take, TicketStatus.new, Role.admin, False)


def test_support_cannot_take() -> None:
    assert not is_action_allowed(TicketAction.take, TicketStatus.new, Role.support, False)


def test_roleless_cannot_take() -> None:
    assert not is_action_allowed(TicketAction.take, TicketStatus.new, None, False)


def test_non_assignee_developer_cannot_resolve() -> None:
    assert not is_action_allowed(
        TicketAction.resolve, TicketStatus.in_progress, Role.developer, False
    )


def test_assignee_developer_can_resolve() -> None:
    assert is_action_allowed(TicketAction.resolve, TicketStatus.in_progress, Role.developer, True)


def test_assignee_developer_can_abandon() -> None:
    assert is_action_allowed(TicketAction.abandon, TicketStatus.in_progress, Role.developer, True)


def test_non_assignee_developer_cannot_abandon() -> None:
    assert not is_action_allowed(
        TicketAction.abandon, TicketStatus.in_progress, Role.developer, False
    )


@pytest.mark.parametrize("from_status", [TicketStatus.in_progress, TicketStatus.waiting_info])
@pytest.mark.parametrize(
    "action",
    [
        TicketAction.to_waiting,
        TicketAction.to_progress,
        TicketAction.resolve,
        TicketAction.reject,
        TicketAction.duplicate,
        TicketAction.abandon,
    ],
)
def test_admin_can_act_on_active_ticket(action: TicketAction, from_status: TicketStatus) -> None:
    assert is_action_allowed(action, from_status, Role.admin, False)


def test_only_admin_can_reopen() -> None:
    assert is_action_allowed(TicketAction.reopen, TicketStatus.resolved, Role.admin, False)
    assert not is_action_allowed(TicketAction.reopen, TicketStatus.resolved, Role.developer, False)
    assert not is_action_allowed(TicketAction.reopen, TicketStatus.resolved, Role.support, False)


def test_only_admin_can_reassign() -> None:
    assert is_action_allowed(TicketAction.reassign, TicketStatus.in_progress, Role.admin, False)
    assert not is_action_allowed(
        TicketAction.reassign, TicketStatus.in_progress, Role.developer, False
    )
    # even the assignee (non-admin) may not reassign
    assert not is_action_allowed(
        TicketAction.reassign, TicketStatus.waiting_info, Role.developer, True
    )
