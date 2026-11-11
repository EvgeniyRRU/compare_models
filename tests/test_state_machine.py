import pytest

from support_bot.domain.errors import Forbidden, InvalidTransition
from support_bot.domain.roles import Role
from support_bot.domain.state_machine import Action, Status, decide


def test_developer_takes_new_ticket() -> None:
    assert decide(Status.NEW, Action.TAKE, actor_role=Role.DEVELOPER) == Status.IN_PROGRESS


def test_admin_has_developer_rights_to_take() -> None:
    assert decide(Status.NEW, Action.TAKE, actor_role=Role.ADMIN) == Status.IN_PROGRESS


def test_support_cannot_take_ticket() -> None:
    with pytest.raises(Forbidden):
        decide(Status.NEW, Action.TAKE, actor_role=Role.SUPPORT)


def test_new_ticket_cannot_jump_to_final_status() -> None:
    for action in (Action.RESOLVE, Action.REJECT, Action.DUPLICATE):
        with pytest.raises(InvalidTransition):
            decide(Status.NEW, action, actor_role=Role.ADMIN)


def test_assignee_moves_between_in_progress_and_waiting_info() -> None:
    assert (
        decide(Status.IN_PROGRESS, Action.WAIT_INFO, actor_role=Role.DEVELOPER, is_assignee=True)
        == Status.WAITING_INFO
    )
    assert (
        decide(Status.WAITING_INFO, Action.RESUME, actor_role=Role.DEVELOPER, is_assignee=True)
        == Status.IN_PROGRESS
    )


def test_non_assignee_developer_cannot_change_taken_ticket() -> None:
    with pytest.raises(Forbidden):
        decide(Status.IN_PROGRESS, Action.RESOLVE, actor_role=Role.DEVELOPER, is_assignee=False)


def test_admin_can_change_taken_ticket_without_being_assignee() -> None:
    assert (
        decide(Status.IN_PROGRESS, Action.RESOLVE, actor_role=Role.ADMIN, is_assignee=False)
        == Status.RESOLVED
    )


@pytest.mark.parametrize("final", [Action.RESOLVE, Action.REJECT, Action.DUPLICATE])
def test_taken_ticket_can_close_in_any_final_status(final: Action) -> None:
    for start in (Status.IN_PROGRESS, Status.WAITING_INFO):
        result = decide(start, final, actor_role=Role.DEVELOPER, is_assignee=True)
        assert result.is_final


def test_assignee_can_release_ticket_back_to_new() -> None:
    assert (
        decide(Status.IN_PROGRESS, Action.RELEASE, actor_role=Role.DEVELOPER, is_assignee=True)
        == Status.NEW
    )


def test_final_ticket_cannot_be_changed_except_by_reopen() -> None:
    with pytest.raises(InvalidTransition):
        decide(Status.RESOLVED, Action.RESOLVE, actor_role=Role.ADMIN)


def test_only_admin_reopens_closed_ticket() -> None:
    with pytest.raises(Forbidden):
        decide(Status.RESOLVED, Action.REOPEN, actor_role=Role.DEVELOPER, is_assignee=True)
    assert decide(Status.REJECTED, Action.REOPEN, actor_role=Role.ADMIN) == Status.NEW


def test_reopen_is_not_allowed_for_open_ticket() -> None:
    with pytest.raises(InvalidTransition):
        decide(Status.IN_PROGRESS, Action.REOPEN, actor_role=Role.ADMIN, is_assignee=True)


def test_only_admin_reassigns_and_status_is_kept() -> None:
    with pytest.raises(Forbidden):
        decide(Status.IN_PROGRESS, Action.REASSIGN, actor_role=Role.DEVELOPER, is_assignee=True)
    assert decide(Status.WAITING_INFO, Action.REASSIGN, actor_role=Role.ADMIN) == Status.WAITING_INFO


def test_reassign_is_not_allowed_for_new_or_final_ticket() -> None:
    with pytest.raises(InvalidTransition):
        decide(Status.NEW, Action.REASSIGN, actor_role=Role.ADMIN)
    with pytest.raises(InvalidTransition):
        decide(Status.RESOLVED, Action.REASSIGN, actor_role=Role.ADMIN)


def test_role_helpers_match_spec() -> None:
    assert Role.SUPPORT.can_create and Role.ADMIN.can_create
    assert not Role.DEVELOPER.can_create
    assert Role.DEVELOPER.can_be_assignee and Role.ADMIN.can_be_assignee
    assert not Role.SUPPORT.can_be_assignee
