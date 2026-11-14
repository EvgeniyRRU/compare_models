"""Callback-data codec for inline keyboards (pure, no Telegram dependency).

Telegram caps ``callback_data`` at 64 bytes. This module encodes ticket and
draft-form state into compact, reversible ``callback_data`` strings and decodes
them back, returning ``None`` for anything malformed so a tampered or truncated
payload can never crash the bot.
"""

import uuid
from dataclasses import dataclass
from typing import Literal

from app.db.enums import Priority
from app.services.state_machine import TicketAction

#: Telegram's hard limit on ``callback_data`` length, in bytes.
MAX_CALLBACK_DATA = 64

#: Single-letter action codes used in ticket callbacks. ``reopen`` and
#: ``reassign`` are admin/Mini-App-only actions with no callback code.
TICKET_ACTION_BY_CODE: dict[str, TicketAction] = {
    "t": TicketAction.take,
    "w": TicketAction.to_waiting,
    "p": TicketAction.to_progress,
    "r": TicketAction.resolve,
    "j": TicketAction.reject,
    "d": TicketAction.duplicate,
    "a": TicketAction.abandon,
}

#: Reverse map: the callback code for each coded action.
CODE_BY_TICKET_ACTION: dict[TicketAction, str] = {
    action: code for code, action in TICKET_ACTION_BY_CODE.items()
}


@dataclass(frozen=True)
class TicketCallback:
    """A decoded ticket-action callback."""

    action: TicketAction
    ticket_id: int
    version: int


@dataclass(frozen=True)
class FormCallback:
    """A decoded draft-form callback."""

    action: str  # "priority" | "skip" | "ok" | "no"
    draft_id: uuid.UUID
    version: int
    priority: Priority | None = None  # set only when action == "priority"


def _check_length(data: str) -> None:
    if len(data.encode("utf-8")) > MAX_CALLBACK_DATA:
        raise ValueError(f"callback_data exceeds {MAX_CALLBACK_DATA} bytes")


def _parse_int(token: str) -> int | None:
    """Parse a strict ASCII decimal token, rejecting signs/whitespace/unicodes."""
    if not (token.isascii() and token.isdigit()):
        return None
    return int(token)


def encode_ticket_callback(action: TicketAction, ticket_id: int, version: int) -> str:
    """Encode a ticket action into ``t:<code>:<ticket_id>:<version>``."""
    code = CODE_BY_TICKET_ACTION[action]
    data = f"t:{code}:{ticket_id}:{version}"
    _check_length(data)
    return data


def decode_ticket_callback(data: str) -> TicketCallback | None:
    """Decode a ticket callback, returning ``None`` when malformed."""
    parts = data.split(":")
    if len(parts) != 4 or parts[0] != "t":
        return None
    action = TICKET_ACTION_BY_CODE.get(parts[1])
    if action is None:
        return None
    ticket_id = _parse_int(parts[2])
    version = _parse_int(parts[3])
    if ticket_id is None or version is None:
        return None
    return TicketCallback(action=action, ticket_id=ticket_id, version=version)


def encode_form_callback(
    action: Literal["priority", "skip", "ok", "no"],
    draft_id: uuid.UUID,
    version: int,
    priority: Priority | None = None,
) -> str:
    """Encode a draft-form callback into ``f:<token>:<draft_id_hex>:<version>``.

    For ``action == "priority"`` the token is the priority value (e.g.
    ``"critical"``); otherwise the token is the action itself (``"skip"``,
    ``"ok"``, or ``"no"``).
    """
    if action == "priority":
        if priority is None:
            raise ValueError("priority is required when action == 'priority'")
        token = priority.value
    elif action in ("skip", "ok", "no"):
        token = action
    else:
        raise ValueError(f"unknown form action: {action!r}")
    data = f"f:{token}:{draft_id.hex}:{version}"
    _check_length(data)
    return data


def decode_form_callback(data: str) -> FormCallback | None:
    """Decode a draft-form callback, returning ``None`` when malformed."""
    parts = data.split(":")
    if len(parts) != 4 or parts[0] != "f":
        return None
    token = parts[1]
    version = _parse_int(parts[3])
    if version is None:
        return None
    try:
        draft_id = uuid.UUID(hex=parts[2])
    except ValueError:
        return None
    if token in {"skip", "ok", "no"}:
        return FormCallback(action=token, draft_id=draft_id, version=version)
    try:
        priority = Priority(token)
    except ValueError:
        return None
    return FormCallback(action="priority", draft_id=draft_id, version=version, priority=priority)
