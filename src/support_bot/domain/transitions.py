"""Explicit ticket state machine, independent of storage and transports."""

from support_bot.domain.errors import BusinessError, Forbidden

ACTIVE = frozenset({"in_progress", "waiting_info"})
FINAL = frozenset({"resolved", "rejected", "duplicate"})
TRANSITIONS = {
    "take": ({"new"}, "in_progress", "taken"),
    "wait": ({"in_progress"}, "waiting_info", "status_changed"),
    "work": ({"waiting_info"}, "in_progress", "status_changed"),
    "resolve": (ACTIVE, "resolved", "status_changed"),
    "reject": (ACTIVE, "rejected", "status_changed"),
    "duplicate": (ACTIVE, "duplicate", "status_changed"),
    "refuse": (ACTIVE, "new", "refused"),
    "reopen": (FINAL, "new", "reopened"),
    "reassign": (ACTIVE, None, "reassigned"),
}


def authorize_transition(
    action: str,
    role: str | None,
    actor_id: int,
    assignee_id: int | None,
) -> None:
    if role not in {"admin", "developer"}:
        raise Forbidden()
    if action in {"reopen", "reassign"} and role != "admin":
        raise Forbidden()
    if action not in {"take", "reopen", "reassign"} and role != "admin" and actor_id != assignee_id:
        raise Forbidden()


def transition(
    status: str,
    action: str,
    role: str | None,
    actor_id: int,
    assignee_id: int | None,
) -> tuple[str, str]:
    authorize_transition(action, role, actor_id, assignee_id)
    edge = TRANSITIONS.get(action)
    if edge is None or status not in edge[0]:
        raise BusinessError("Действие недоступно в текущем статусе")
    return edge[1] or status, edge[2]
