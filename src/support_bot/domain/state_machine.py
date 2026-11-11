from collections.abc import Callable
from enum import StrEnum

from support_bot.domain.errors import Forbidden, InvalidTransition
from support_bot.domain.roles import Role


class Status(StrEnum):
    NEW = "new"
    IN_PROGRESS = "in_progress"
    WAITING_INFO = "waiting_info"
    RESOLVED = "resolved"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"

    @property
    def is_final(self) -> bool:
        return self in (Status.RESOLVED, Status.REJECTED, Status.DUPLICATE)


class Action(StrEnum):
    TAKE = "take"
    WAIT_INFO = "wait_info"
    RESUME = "resume"
    RESOLVE = "resolve"
    REJECT = "reject"
    DUPLICATE = "duplicate"
    RELEASE = "release"
    REOPEN = "reopen"
    REASSIGN = "reassign"


Permission = Callable[[Role, bool], bool]


def _taker(role: Role, _is_assignee: bool) -> bool:
    return role.can_take


def _assignee_or_admin(role: Role, is_assignee: bool) -> bool:
    return is_assignee or role is Role.ADMIN


def _admin(role: Role, _is_assignee: bool) -> bool:
    return role is Role.ADMIN


_TAKEN = (Status.IN_PROGRESS, Status.WAITING_INFO)
_KEEP: None = None

# (current status, action) -> (target status or None to keep the status, permission)
_RULES: dict[tuple[Status, Action], tuple[Status | None, Permission]] = {
    (Status.NEW, Action.TAKE): (Status.IN_PROGRESS, _taker),
    (Status.IN_PROGRESS, Action.WAIT_INFO): (Status.WAITING_INFO, _assignee_or_admin),
    (Status.WAITING_INFO, Action.RESUME): (Status.IN_PROGRESS, _assignee_or_admin),
    (Status.IN_PROGRESS, Action.RELEASE): (Status.NEW, _assignee_or_admin),
    (Status.WAITING_INFO, Action.RELEASE): (Status.NEW, _assignee_or_admin),
    (Status.REJECTED, Action.REOPEN): (Status.NEW, _admin),
    (Status.RESOLVED, Action.REOPEN): (Status.NEW, _admin),
    (Status.DUPLICATE, Action.REOPEN): (Status.NEW, _admin),
    (Status.IN_PROGRESS, Action.REASSIGN): (_KEEP, _admin),
    (Status.WAITING_INFO, Action.REASSIGN): (_KEEP, _admin),
}
for _taken in _TAKEN:
    for _final_action, _final in (
        (Action.RESOLVE, Status.RESOLVED),
        (Action.REJECT, Status.REJECTED),
        (Action.DUPLICATE, Status.DUPLICATE),
    ):
        _RULES[(_taken, _final_action)] = (_final, _assignee_or_admin)


def decide(
    status: Status,
    action: Action,
    *,
    actor_role: Role,
    is_assignee: bool = False,
) -> Status:
    """Return the status after `action`, or raise if the action is not allowed."""
    rule = _RULES.get((status, action))
    if rule is None:
        raise InvalidTransition(f"Действие {action} недоступно в статусе {status}")
    target, permission = rule
    if not permission(actor_role, is_assignee):
        raise Forbidden("Недостаточно прав для этого действия")
    return status if target is None else target
