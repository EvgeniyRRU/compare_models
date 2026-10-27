import pytest

from support_bot.domain.errors import BusinessError, Forbidden
from support_bot.domain.transitions import transition


@pytest.mark.parametrize(
    ("status", "action", "target", "event"),
    [
        ("new", "take", "in_progress", "taken"),
        ("in_progress", "wait", "waiting_info", "status_changed"),
        ("waiting_info", "work", "in_progress", "status_changed"),
        ("in_progress", "resolve", "resolved", "status_changed"),
        ("waiting_info", "reject", "rejected", "status_changed"),
        ("in_progress", "duplicate", "duplicate", "status_changed"),
        ("waiting_info", "refuse", "new", "refused"),
        ("resolved", "reopen", "new", "reopened"),
        ("waiting_info", "reassign", "waiting_info", "reassigned"),
    ],
)
def test_explicit_transitions(status, action, target, event):
    assert transition(status, action, "admin", 1, 1) == (target, event)


@pytest.mark.parametrize(
    "status", ["new", "in_progress", "waiting_info", "resolved", "rejected", "duplicate"]
)
@pytest.mark.parametrize(
    "action",
    ["take", "wait", "work", "resolve", "reject", "duplicate", "refuse", "reopen", "reassign"],
)
def test_state_machine_rejects_every_undefined_edge(status, action):
    allowed = {
        "take": {"new"},
        "wait": {"in_progress"},
        "work": {"waiting_info"},
        "resolve": {"in_progress", "waiting_info"},
        "reject": {"in_progress", "waiting_info"},
        "duplicate": {"in_progress", "waiting_info"},
        "refuse": {"in_progress", "waiting_info"},
        "reopen": {"resolved", "rejected", "duplicate"},
        "reassign": {"in_progress", "waiting_info"},
    }
    if status not in allowed[action]:
        with pytest.raises(BusinessError):
            transition(status, action, "admin", 1, 1)


@pytest.mark.parametrize(
    ("role", "actor", "assignee", "action"),
    [
        ("support", 1, None, "take"),
        (None, 1, None, "take"),
        ("developer", 2, 1, "wait"),
        ("developer", 1, 1, "reopen"),
        ("developer", 1, 1, "reassign"),
        ("support", 1, 1, "resolve"),
    ],
)
def test_role_and_assignment_permissions(role, actor, assignee, action):
    status = "new" if action == "take" else "resolved" if action == "reopen" else "in_progress"
    with pytest.raises(Forbidden):
        transition(status, action, role, actor, assignee)
