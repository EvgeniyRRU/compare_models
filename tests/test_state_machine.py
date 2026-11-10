import pytest

from app.domain.state_machine import InvalidTransition, allowed_transition


@pytest.mark.parametrize("status", ["resolved", "rejected", "duplicate"])
def test_new_cannot_be_closed(status):
    with pytest.raises(InvalidTransition):
        allowed_transition("new", status, "admin", False)


def test_admin_can_take_ticket():
    allowed_transition("new", "in_progress", "admin", False)


def test_only_assignee_or_admin_can_change_active_ticket():
    with pytest.raises(InvalidTransition):
        allowed_transition("in_progress", "resolved", "developer", False)
    allowed_transition("in_progress", "resolved", "developer", True)
    allowed_transition("in_progress", "resolved", "admin", False)


def test_only_admin_can_reopen():
    with pytest.raises(InvalidTransition):
        allowed_transition("resolved", "new", "developer", True)
    allowed_transition("resolved", "new", "admin", False)
