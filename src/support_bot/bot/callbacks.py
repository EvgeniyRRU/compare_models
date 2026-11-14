from __future__ import annotations

import base64
import uuid

DRAFT_PREFIX = "d"
TICKET_PREFIX = "t"
CALLBACK_LIMIT = 64


def encode_draft_id(draft_id: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(draft_id.bytes).rstrip(b"=").decode("ascii")


def decode_draft_id(value: str) -> uuid.UUID:
    padding = "=" * (-len(value) % 4)
    return uuid.UUID(bytes=base64.urlsafe_b64decode(value + padding))


def draft_callback(draft_id: uuid.UUID, version: int, action: str) -> str:
    data = f"{DRAFT_PREFIX}:{encode_draft_id(draft_id)}:{version}:{action}"
    if len(data.encode()) > CALLBACK_LIMIT:
        raise ValueError("callback_data is too long")
    return data


def parse_draft_callback(data: str) -> tuple[uuid.UUID, int, str] | None:
    parts = data.split(":")
    if len(parts) != 4 or parts[0] != DRAFT_PREFIX:
        return None
    try:
        return decode_draft_id(parts[1]), int(parts[2]), parts[3]
    except ValueError, TypeError:
        return None


def ticket_callback(ticket_id: int, version: int, action: str) -> str:
    data = f"{TICKET_PREFIX}:{action}:{ticket_id}:{version}"
    if len(data.encode()) > CALLBACK_LIMIT:
        raise ValueError("callback_data is too long")
    return data


def parse_ticket_callback(data: str) -> tuple[str, int, int] | None:
    parts = data.split(":")
    if len(parts) != 4 or parts[0] != TICKET_PREFIX:
        return None
    try:
        return parts[1], int(parts[2]), int(parts[3])
    except ValueError:
        return None
