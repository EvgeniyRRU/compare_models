"""Pure state-machine matrix — no DB."""

import pytest

from app.db.models import EventType, Role, TicketStatus
from app.domain import state_machine as sm
from app.domain.state_machine import Action

NEW, PROG, WAIT = TicketStatus.new, TicketStatus.in_progress, TicketStatus.waiting_info
RES, REJ, DUP = TicketStatus.resolved, TicketStatus.rejected, TicketStatus.duplicate

ADMIN_ID = 1


class TestSpecTargets:
    @pytest.mark.parametrize(
        ("action", "expected"),
        [
            (Action.take, PROG),
            (Action.to_waiting, WAIT),
            (Action.to_progress, PROG),
            (Action.resolve, RES),
            (Action.reject, REJ),
            (Action.duplicate, DUP),
            (Action.abandon, NEW),
            (Action.reopen, NEW),
            (Action.reassign, None),  # status unchanged
        ],
    )
    def test_target_status(self, action, expected):
        assert sm.spec(action).target_status is expected

    def test_event_types(self):
        assert sm.spec(Action.take).event_type is EventType.taken
        assert sm.spec(Action.abandon).event_type is EventType.abandoned
        assert sm.spec(Action.reopen).event_type is EventType.reopened
        assert sm.spec(Action.reassign).event_type is EventType.reassigned
        assert sm.spec(Action.resolve).event_type is EventType.status_changed


class TestTransitions:
    def test_take_only_from_new(self):
        assert sm.is_transition_allowed(Action.take, NEW)
        for st in (PROG, WAIT, RES, REJ, DUP):
            assert not sm.is_transition_allowed(Action.take, st)

    def test_to_waiting_only_from_progress(self):
        assert sm.is_transition_allowed(Action.to_waiting, PROG)
        assert not sm.is_transition_allowed(Action.to_waiting, WAIT)
        assert not sm.is_transition_allowed(Action.to_waiting, NEW)

    def test_to_progress_only_from_waiting(self):
        assert sm.is_transition_allowed(Action.to_progress, WAIT)
        assert not sm.is_transition_allowed(Action.to_progress, PROG)

    @pytest.mark.parametrize(
        "action", [Action.resolve, Action.reject, Action.duplicate, Action.abandon, Action.reassign]
    )
    def test_closure_actions_from_active_only(self, action):
        for st in (PROG, WAIT):
            assert sm.is_transition_allowed(action, st)
        for st in (NEW, RES, REJ, DUP):
            assert not sm.is_transition_allowed(action, st)

    def test_reopen_only_from_final(self):
        for st in (RES, REJ, DUP):
            assert sm.is_transition_allowed(Action.reopen, st)
        for st in (NEW, PROG, WAIT):
            assert not sm.is_transition_allowed(Action.reopen, st)

    def test_entering_leaving_new(self):
        assert sm.entering_new(Action.abandon)
        assert sm.entering_new(Action.reopen)
        assert not sm.entering_new(Action.take)
        assert sm.leaving_new(Action.take)
        assert not sm.leaving_new(Action.abandon)


class TestCanActor:
    def test_no_role_denies_everything(self):
        for action in Action:
            assert not sm.can_actor(action, None, 42, None)
            assert not sm.can_actor(action, None, 42, 42)

    def test_support_cannot_act_as_executor(self):
        for action in Action:
            assert not sm.can_actor(action, Role.support, 42, 42)

    def test_take_allowed_for_executor_roles(self):
        assert sm.can_actor(Action.take, Role.developer, 42, None)
        assert sm.can_actor(Action.take, Role.admin, 42, None)

    @pytest.mark.parametrize(
        "action",
        [Action.to_waiting, Action.resolve, Action.reject, Action.duplicate, Action.abandon],
    )
    def test_assignee_scoped(self, action):
        assignee = 42
        stranger = 43
        assert sm.can_actor(action, Role.developer, assignee, assignee)
        assert not sm.can_actor(action, Role.developer, stranger, assignee)
        assert not sm.can_actor(action, Role.developer, stranger, None)
        assert sm.can_actor(action, Role.admin, stranger, assignee)  # admin superset

    @pytest.mark.parametrize("action", [Action.reopen, Action.reassign])
    def test_admin_only(self, action):
        assert sm.can_actor(action, Role.admin, ADMIN_ID, None)
        assert not sm.can_actor(action, Role.developer, ADMIN_ID, None)


class TestButtons:
    def test_new_shows_take_only(self):
        assert sm.available_buttons(NEW) == [Action.take]

    def test_waiting_buttons(self):
        buttons = sm.available_buttons(WAIT)
        assert buttons[0] is Action.to_progress
        assert set(buttons) == {
            Action.to_progress,
            Action.resolve,
            Action.reject,
            Action.duplicate,
            Action.abandon,
        }
        assert Action.take not in buttons

    def test_progress_buttons(self):
        buttons = sm.available_buttons(PROG)
        assert buttons[0] is Action.to_waiting
        assert Action.to_progress not in buttons

    @pytest.mark.parametrize("status", [RES, REJ, DUP])
    def test_final_statuses_have_no_buttons(self, status):
        assert sm.available_buttons(status) == []
