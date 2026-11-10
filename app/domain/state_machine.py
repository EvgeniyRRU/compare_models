"""Authorization and allowed status transitions for tickets."""


class InvalidTransition(ValueError):
    """The requested transition or the actor's role is invalid."""


def allowed_transition(current: str, target: str, role: str | None, assignee: bool) -> None:
    active = {"in_progress", "waiting_info"}
    final = {"resolved", "rejected", "duplicate"}
    if current == "new" and target == "in_progress" and role in {"developer", "admin"}:
        return
    if current in active and target in (active - {current}) | final | {"new"}:
        if role == "admin" or (role == "developer" and assignee):
            return
    if current in final and target == "new" and role == "admin":
        return
    raise InvalidTransition(f"Cannot transition from {current} to {target}")
