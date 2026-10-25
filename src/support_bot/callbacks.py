"""Компактный формат callback_data (лимит Telegram — 64 байта).

Пост обращения: ``t:<действие>:<id>:<version>``, например ``t:tk:123:5``.
Форма черновика: ``d:<действие>:<uuid base64url, 22 символа>:<version>[:<аргумент>]``.
"""

import base64
import uuid
from dataclasses import dataclass
from enum import StrEnum

from support_bot.domain.enums import Priority
from support_bot.domain.state_machine import Action

MAX_CALLBACK_BYTES = 64

TICKET_ACTION_CODES: dict[Action, str] = {
    Action.TAKE: "tk",
    Action.TO_WAITING: "wi",
    Action.TO_WORK: "ip",
    Action.RESOLVE: "rs",
    Action.REJECT: "rj",
    Action.DUPLICATE: "dp",
    Action.RELEASE: "rl",
}
_TICKET_CODE_ACTIONS = {code: action for action, code in TICKET_ACTION_CODES.items()}

PRIORITY_CODES: dict[Priority, str] = {
    Priority.CRITICAL: "c",
    Priority.HIGH: "h",
    Priority.MEDIUM: "m",
    Priority.LOW: "l",
}
_PRIORITY_BY_CODE = {code: p for p, code in PRIORITY_CODES.items()}


class DraftAction(StrEnum):
    PRIORITY = "p"
    SKIP_TENANT = "s"
    SUBMIT = "ok"
    CANCEL = "x"
    CONTINUE = "c"
    RESTART = "r"


class CallbackDataError(ValueError):
    pass


def encode_uuid(value: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(value.bytes).rstrip(b"=").decode("ascii")


def decode_uuid(value: str) -> uuid.UUID:
    if len(value) != 22:
        raise CallbackDataError("bad uuid")
    try:
        return uuid.UUID(bytes=base64.urlsafe_b64decode(value + "=="))
    except (ValueError, TypeError) as exc:
        raise CallbackDataError("bad uuid") from exc


def _checked(data: str) -> str:
    if len(data.encode("utf-8")) > MAX_CALLBACK_BYTES:
        raise CallbackDataError(f"callback_data longer than {MAX_CALLBACK_BYTES} bytes")
    return data


@dataclass(frozen=True)
class TicketCallback:
    action: Action
    ticket_id: int
    version: int

    def encode(self) -> str:
        return _checked(f"t:{TICKET_ACTION_CODES[self.action]}:{self.ticket_id}:{self.version}")


@dataclass(frozen=True)
class DraftCallback:
    action: DraftAction
    draft_id: uuid.UUID
    version: int
    priority: Priority | None = None

    def encode(self) -> str:
        parts = ["d", self.action.value, encode_uuid(self.draft_id), str(self.version)]
        if self.action is DraftAction.PRIORITY:
            if self.priority is None:
                raise CallbackDataError("priority required")
            parts.append(PRIORITY_CODES[self.priority])
        return _checked(":".join(parts))


def _int(value: str) -> int:
    if not value.isdigit():
        raise CallbackDataError("bad int")
    return int(value)


def parse_ticket_callback(data: str) -> TicketCallback:
    parts = data.split(":")
    if len(parts) != 4 or parts[0] != "t" or parts[1] not in _TICKET_CODE_ACTIONS:
        raise CallbackDataError("bad ticket callback")
    return TicketCallback(_TICKET_CODE_ACTIONS[parts[1]], _int(parts[2]), _int(parts[3]))


def parse_draft_callback(data: str) -> DraftCallback:
    parts = data.split(":")
    if len(parts) < 4 or parts[0] != "d":
        raise CallbackDataError("bad draft callback")
    try:
        action = DraftAction(parts[1])
    except ValueError as exc:
        raise CallbackDataError("bad draft action") from exc
    draft_id = decode_uuid(parts[2])
    version = _int(parts[3])
    if action is DraftAction.PRIORITY:
        if len(parts) != 5 or parts[4] not in _PRIORITY_BY_CODE:
            raise CallbackDataError("bad priority")
        return DraftCallback(action, draft_id, version, _PRIORITY_BY_CODE[parts[4]])
    if len(parts) != 4:
        raise CallbackDataError("bad draft callback")
    return DraftCallback(action, draft_id, version)
