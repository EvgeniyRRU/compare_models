import pytest

from app.domain.state_machine import (
    Action,
    Actor,
    AlreadyTaken,
    InvalidTransition,
    PermissionDenied,
    Role,
    Status,
    plan_transition,
)

DEV = Actor(2, Role.DEVELOPER)
OTHER_DEV = Actor(3, Role.DEVELOPER)
ADMIN = Actor(4, Role.ADMIN)
SUPPORT = Actor(1, Role.SUPPORT)


def test_take_by_developer_and_admin():
    for actor in (DEV, ADMIN):
        tr = plan_transition(Status.NEW, None, Action.TAKE, actor)
        assert tr.new_status == Status.IN_PROGRESS
        assert (tr.assignee, tr.taken_at, tr.reminder) == ("set", "set", "clear")


def test_support_cannot_take():
    with pytest.raises(PermissionDenied):
        plan_transition(Status.NEW, None, Action.TAKE, SUPPORT)


def test_take_when_not_new_is_already_taken():
    with pytest.raises(AlreadyTaken):
        plan_transition(Status.IN_PROGRESS, 3, Action.TAKE, DEV)


@pytest.mark.parametrize("action", [Action.RESOLVE, Action.REJECT, Action.DUPLICATE])
def test_new_cannot_go_to_final(action):
    with pytest.raises(InvalidTransition):
        plan_transition(Status.NEW, None, action, ADMIN)


@pytest.mark.parametrize("status", [Status.IN_PROGRESS, Status.WAITING_INFO])
@pytest.mark.parametrize(
    ("action", "expected"),
    [
        (Action.RESOLVE, Status.RESOLVED),
        (Action.REJECT, Status.REJECTED),
        (Action.DUPLICATE, Status.DUPLICATE),
    ],
)
def test_final_transitions(status, action, expected):
    tr = plan_transition(status, 2, action, DEV)
    assert tr.new_status == expected and tr.closed_at == "set"


def test_wait_resume_cycle():
    assert plan_transition(Status.IN_PROGRESS, 2, Action.WAIT, DEV).new_status == Status.WAITING_INFO
    assert plan_transition(Status.WAITING_INFO, 2, Action.RESUME, DEV).new_status == Status.IN_PROGRESS
    with pytest.raises(InvalidTransition):
        plan_transition(Status.WAITING_INFO, 2, Action.WAIT, DEV)


def test_other_developer_cannot_act_but_admin_can():
    with pytest.raises(PermissionDenied):
        plan_transition(Status.IN_PROGRESS, 2, Action.RESOLVE, OTHER_DEV)
    assert plan_transition(Status.IN_PROGRESS, 2, Action.RESOLVE, ADMIN).new_status == Status.RESOLVED


def test_release_returns_to_new_and_schedules_reminder():
    tr = plan_transition(Status.WAITING_INFO, 2, Action.RELEASE, DEV)
    assert (tr.new_status, tr.assignee, tr.taken_at, tr.reminder) == (
        Status.NEW, "clear", "clear", "schedule",
    )


@pytest.mark.parametrize("status", [Status.RESOLVED, Status.REJECTED, Status.DUPLICATE])
def test_reopen_only_admin(status):
    with pytest.raises(PermissionDenied):
        plan_transition(status, 2, Action.REOPEN, DEV)
    tr = plan_transition(status, 2, Action.REOPEN, ADMIN)
    assert (tr.new_status, tr.closed_at, tr.reminder) == (Status.NEW, "clear", "schedule")


def test_reopen_only_closed():
    with pytest.raises(InvalidTransition):
        plan_transition(Status.IN_PROGRESS, 2, Action.REOPEN, ADMIN)


def test_reassign_only_admin_and_active():
    with pytest.raises(PermissionDenied):
        plan_transition(Status.IN_PROGRESS, 2, Action.REASSIGN, DEV, 3)
    with pytest.raises(InvalidTransition):
        plan_transition(Status.NEW, None, Action.REASSIGN, ADMIN, 3)
    with pytest.raises(InvalidTransition):
        plan_transition(Status.IN_PROGRESS, 3, Action.REASSIGN, ADMIN, 3)
    tr = plan_transition(Status.WAITING_INFO, 2, Action.REASSIGN, ADMIN, 3)
    assert (tr.new_status, tr.taken_at) == (Status.WAITING_INFO, "set")


def test_user_without_role_has_no_rights():
    nobody = Actor(5, None)
    with pytest.raises(PermissionDenied):
        plan_transition(Status.IN_PROGRESS, 5, Action.RESOLVE, nobody)
