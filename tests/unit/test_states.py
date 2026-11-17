import pytest

from app.services.errors import PermissionDenied
from app.states import FINAL_STATUSES, TRANSITIONS, check_permission


def test_take_from_new_only_by_executors():
    t = TRANSITIONS["take"]
    assert t.from_statuses == frozenset({"new"})
    assert t.to_status == "in_progress"
    assert t.roles == frozenset({"developer", "admin"})
    check_permission("developer", 1, None, t)
    check_permission("admin", 1, None, t)
    with pytest.raises(PermissionDenied):
        check_permission("support", 1, None, t)
    with pytest.raises(PermissionDenied):
        check_permission(None, 1, None, t)


def test_no_final_transition_from_new():
    for action in ("to_resolved", "to_rejected", "to_duplicate"):
        t = TRANSITIONS[action]
        assert "new" not in t.from_statuses
        assert t.to_status in FINAL_STATUSES
        assert t.sets_closed_at


def test_reopen_and_reassign_admin_only():
    assert TRANSITIONS["reopen"].roles == frozenset({"admin"})
    assert TRANSITIONS["reassign"].roles == frozenset({"admin"})
    for action in ("reopen", "reassign"):
        with pytest.raises(PermissionDenied):
            check_permission("developer", 1, None, TRANSITIONS[action])


def test_active_transitions_assignee_or_admin():
    t = TRANSITIONS["to_waiting"]
    check_permission("admin", 99, 5, t)
    check_permission("developer", 5, 5, t)
    with pytest.raises(PermissionDenied):
        check_permission("developer", 6, 5, t)


def test_decline_and_reopen_clear_fields():
    d = TRANSITIONS["decline"]
    assert (d.to_status, d.clears_assignee, d.clears_taken_at, d.sets_reminder) == (
        "new",
        True,
        True,
        True,
    )
    r = TRANSITIONS["reopen"]
    assert (
        r.to_status,
        r.clears_assignee,
        r.clears_taken_at,
        r.clears_closed_at,
        r.sets_reminder,
    ) == ("new", True, True, True, True)


def test_reassign_keeps_status_resets_taken_at():
    r = TRANSITIONS["reassign"]
    assert r.to_status is None
    assert r.sets_taken_at
    assert r.from_statuses == frozenset({"in_progress", "waiting_info"})
