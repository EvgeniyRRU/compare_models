"""Pure ticket state machine: transitions and permission rules.

This module has no I/O — it depends only on the enum types and is used by the
ticket service (and, later, the bot/API layers) to decide which transitions are
legal and who may perform them.
"""

from enum import StrEnum

from app.db.enums import Role, TicketStatus


class TicketAction(StrEnum):
    """Actions a user may request against a ticket."""

    take = "take"
    to_waiting = "to_waiting"
    to_progress = "to_progress"
    resolve = "resolve"
    reject = "reject"
    duplicate = "duplicate"
    abandon = "abandon"
    reopen = "reopen"
    reassign = "reassign"


#: The authoritative transition table: ``(from_status, action) -> to_status``.
TRANSITIONS: dict[tuple[TicketStatus, TicketAction], TicketStatus] = {
    (TicketStatus.new, TicketAction.take): TicketStatus.in_progress,
    (TicketStatus.in_progress, TicketAction.to_waiting): TicketStatus.waiting_info,
    (TicketStatus.waiting_info, TicketAction.to_progress): TicketStatus.in_progress,
    (TicketStatus.in_progress, TicketAction.resolve): TicketStatus.resolved,
    (TicketStatus.in_progress, TicketAction.reject): TicketStatus.rejected,
    (TicketStatus.in_progress, TicketAction.duplicate): TicketStatus.duplicate,
    (TicketStatus.waiting_info, TicketAction.resolve): TicketStatus.resolved,
    (TicketStatus.waiting_info, TicketAction.reject): TicketStatus.rejected,
    (TicketStatus.waiting_info, TicketAction.duplicate): TicketStatus.duplicate,
    (TicketStatus.in_progress, TicketAction.abandon): TicketStatus.new,
    (TicketStatus.waiting_info, TicketAction.abandon): TicketStatus.new,
    (TicketStatus.resolved, TicketAction.reopen): TicketStatus.new,
    (TicketStatus.rejected, TicketAction.reopen): TicketStatus.new,
    (TicketStatus.duplicate, TicketAction.reopen): TicketStatus.new,
    (TicketStatus.in_progress, TicketAction.reassign): TicketStatus.in_progress,
    (TicketStatus.waiting_info, TicketAction.reassign): TicketStatus.waiting_info,
}


def can_transition(from_status: TicketStatus, action: TicketAction) -> bool:
    """Return whether ``action`` is a legal transition out of ``from_status``."""
    return (from_status, action) in TRANSITIONS


def transition(from_status: TicketStatus, action: TicketAction) -> TicketStatus:
    """Return the status that ``action`` produces, or raise ``ValueError``."""
    try:
        return TRANSITIONS[(from_status, action)]
    except KeyError:
        raise ValueError(
            f"transition {from_status.value} --{action.value}--> is not allowed"
        ) from None


def is_action_allowed(
    action: TicketAction,
    from_status: TicketStatus,
    actor_role: Role | None,
    is_assignee: bool,
) -> bool:
    """Return whether *actor* may request *action* on a ticket currently in
    ``from_status``.

    ``from_status`` is accepted for API symmetry with :func:`can_transition`
    (and is the value a caller already has to hand when deciding which buttons
    to render), but the permission rules below branch only on ``actor_role``
    and ``is_assignee``:

    * ``take`` — ``developer`` or ``admin``.
    * ``to_waiting`` / ``to_progress`` / ``resolve`` / ``reject`` / ``duplicate``
      / ``abandon`` — ``admin`` or the current assignee.
    * ``reopen`` / ``reassign`` — ``admin`` only.

    Whether ``action`` is even a legal transition out of ``from_status`` is a
    separate concern answered by :func:`can_transition`; the service layer
    enforces it with a conditional ``UPDATE`` whose ``status`` guard mirrors
    the transition table.
    """
    if action == TicketAction.take:
        return actor_role in (Role.developer, Role.admin)
    if action in (TicketAction.reopen, TicketAction.reassign):
        return actor_role == Role.admin
    return actor_role == Role.admin or is_assignee
