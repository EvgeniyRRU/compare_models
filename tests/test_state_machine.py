import pytest

from support_bot.domain.enums import FINAL_STATUSES, Role, TicketStatus
from support_bot.domain.errors import (
    AccessDeniedError,
    InvalidAssigneeError,
    InvalidTransitionError,
    PermissionDeniedError,
)
from support_bot.domain.state_machine import (
    POST_ACTIONS,
    TRANSITIONS,
    Action,
    ReminderOp,
    TimeOp,
    can_be_assignee,
    can_create,
    check_permission,
    check_reassign,
    get_transition,
    plan_transition,
)

S = TicketStatus
ALLOWED = {
    (Action.TAKE, S.NEW): S.IN_PROGRESS,
    (Action.TO_WAITING, S.IN_PROGRESS): S.WAITING_INFO,
    (Action.TO_WORK, S.WAITING_INFO): S.IN_PROGRESS,
    **{
        (a, s): t
        for a, t in (
            (Action.RESOLVE, S.RESOLVED),
            (Action.REJECT, S.REJECTED),
            (Action.DUPLICATE, S.DUPLICATE),
            (Action.RELEASE, S.NEW),
        )
        for s in (S.IN_PROGRESS, S.WAITING_INFO)
    },
    **{(Action.REOPEN, s): S.NEW for s in FINAL_STATUSES},
}


@pytest.mark.parametrize("action", list(Action))
@pytest.mark.parametrize("status", list(TicketStatus))
def test_transition_table(action: Action, status: TicketStatus) -> None:
    expected = ALLOWED.get((action, status))
    if expected is None:
        with pytest.raises(InvalidTransitionError):
            get_transition(action, status)
    else:
        assert get_transition(action, status).target is expected


@pytest.mark.parametrize("action", [Action.RESOLVE, Action.REJECT, Action.DUPLICATE])
def test_new_cannot_go_to_final(action: Action) -> None:
    with pytest.raises(InvalidTransitionError):
        get_transition(action, S.NEW)


def test_roles() -> None:
    assert can_create(Role.SUPPORT) and can_create(Role.ADMIN)
    assert not can_create(Role.DEVELOPER) and not can_create(None)
    assert can_be_assignee(Role.DEVELOPER) and can_be_assignee(Role.ADMIN)
    assert not can_be_assignee(Role.SUPPORT) and not can_be_assignee(None)


@pytest.mark.parametrize(
    ("role", "allowed"),
    [(Role.DEVELOPER, True), (Role.ADMIN, True), (Role.SUPPORT, False)],
)
def test_take_permission(role: Role, allowed: bool) -> None:
    t = TRANSITIONS[Action.TAKE]
    if allowed:
        check_permission(t, 1, role, None)
    else:
        with pytest.raises(PermissionDeniedError):
            check_permission(t, 1, role, None)


def test_no_role_is_access_denied() -> None:
    with pytest.raises(AccessDeniedError):
        check_permission(TRANSITIONS[Action.TAKE], 1, None, None)


@pytest.mark.parametrize("action", [Action.TO_WAITING, Action.RESOLVE, Action.RELEASE])
def test_assignee_or_admin(action: Action) -> None:
    t = TRANSITIONS[action]
    check_permission(t, 7, Role.DEVELOPER, 7)  # исполнитель
    check_permission(t, 1, Role.ADMIN, 7)  # admin как developer и больше
    with pytest.raises(PermissionDeniedError):
        check_permission(t, 8, Role.DEVELOPER, 7)  # чужое обращение
    with pytest.raises(PermissionDeniedError):
        check_permission(t, 8, Role.SUPPORT, 7)


def test_reopen_only_admin() -> None:
    t = TRANSITIONS[Action.REOPEN]
    check_permission(t, 1, Role.ADMIN, 7)
    with pytest.raises(PermissionDeniedError):
        check_permission(t, 7, Role.DEVELOPER, 7)


def test_reassign_rules() -> None:
    changes = check_reassign(S.IN_PROGRESS, Role.ADMIN, 7, 8, Role.DEVELOPER)
    assert changes.assignee_id == 8 and changes.taken_at is TimeOp.NOW
    assert changes.status is S.IN_PROGRESS
    with pytest.raises(PermissionDeniedError):
        check_reassign(S.IN_PROGRESS, Role.DEVELOPER, 7, 8, Role.DEVELOPER)
    with pytest.raises(InvalidTransitionError):
        check_reassign(S.NEW, Role.ADMIN, None, 8, Role.DEVELOPER)
    with pytest.raises(InvalidTransitionError):
        check_reassign(S.RESOLVED, Role.ADMIN, 7, 8, Role.DEVELOPER)
    with pytest.raises(InvalidAssigneeError):
        check_reassign(S.WAITING_INFO, Role.ADMIN, 7, 8, Role.SUPPORT)
    with pytest.raises(InvalidAssigneeError):
        check_reassign(S.WAITING_INFO, Role.ADMIN, 7, 7, Role.DEVELOPER)


def test_plans() -> None:
    take = plan_transition(TRANSITIONS[Action.TAKE], 5)
    assert (take.assignee_id, take.taken_at, take.reminder) == (5, TimeOp.NOW, ReminderOp.CLEAR)
    release = plan_transition(TRANSITIONS[Action.RELEASE], 5)
    assert release.assignee_changed and release.assignee_id is None
    assert (release.taken_at, release.reminder) == (TimeOp.CLEAR, ReminderOp.SCHEDULE)
    reopen = plan_transition(TRANSITIONS[Action.REOPEN], 5)
    assert (reopen.taken_at, reopen.closed_at) == (TimeOp.CLEAR, TimeOp.CLEAR)
    resolve = plan_transition(TRANSITIONS[Action.RESOLVE], 5)
    assert resolve.closed_at is TimeOp.NOW and not resolve.assignee_changed


def test_post_actions_are_valid_transitions() -> None:
    for status, actions in POST_ACTIONS.items():
        for action in actions:
            assert get_transition(action, status)
