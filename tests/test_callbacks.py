"""Tests for the callback-data codec (pure, no database/Telegram)."""

import uuid

import pytest

from app.bot.callbacks import (
    MAX_CALLBACK_DATA,
    TICKET_ACTION_BY_CODE,
    decode_form_callback,
    decode_ticket_callback,
    encode_form_callback,
    encode_ticket_callback,
)
from app.db.enums import Priority
from app.services.state_machine import TicketAction

CODED_ACTIONS = [
    TicketAction.take,
    TicketAction.to_waiting,
    TicketAction.to_progress,
    TicketAction.resolve,
    TicketAction.reject,
    TicketAction.duplicate,
    TicketAction.abandon,
]


@pytest.mark.parametrize("action", CODED_ACTIONS)
def test_ticket_callback_round_trip(action: TicketAction) -> None:
    data = encode_ticket_callback(action, 42, 3)
    decoded = decode_ticket_callback(data)

    assert decoded is not None
    assert decoded.action is action
    assert decoded.ticket_id == 42
    assert decoded.version == 3


def test_ticket_code_map() -> None:
    expected = {
        "t": TicketAction.take,
        "w": TicketAction.to_waiting,
        "p": TicketAction.to_progress,
        "r": TicketAction.resolve,
        "j": TicketAction.reject,
        "d": TicketAction.duplicate,
        "a": TicketAction.abandon,
    }
    assert expected == TICKET_ACTION_BY_CODE


@pytest.mark.parametrize(
    "data",
    [
        "",
        "t",
        "x:t:1:1",  # wrong prefix
        "t:t:1",  # wrong arity (too few)
        "t:t:1:1:extra",  # wrong arity (too many)
        "t:z:1:1",  # unknown code
        "t:t:notanint:1",  # non-int id
        "t:t:1:notanint",  # non-int version
    ],
)
def test_malformed_ticket_callback_is_none(data: str) -> None:
    assert decode_ticket_callback(data) is None


@pytest.mark.parametrize("priority", list(Priority))
def test_form_priority_round_trip(priority: Priority) -> None:
    draft_id = uuid.uuid4()
    data = encode_form_callback("priority", draft_id, 7, priority)
    decoded = decode_form_callback(data)

    assert decoded is not None
    assert decoded.action == "priority"
    assert decoded.priority is priority
    assert decoded.draft_id == draft_id
    assert decoded.version == 7


@pytest.mark.parametrize("action", ["skip", "ok", "no"])
def test_form_simple_round_trip(action: str) -> None:
    draft_id = uuid.uuid4()
    data = encode_form_callback(action, draft_id, 2)
    decoded = decode_form_callback(data)

    assert decoded is not None
    assert decoded.action == action
    assert decoded.priority is None
    assert decoded.draft_id == draft_id
    assert decoded.version == 2


@pytest.mark.parametrize(
    "data",
    [
        "",
        "f",
        "x:skip:00000000000000000000000000000000:1",  # wrong prefix
        "f:skip:00000000000000000000000000000000",  # wrong arity (too few)
        "f:skip:00000000000000000000000000000000:1:2",  # wrong arity (too many)
        "f:unknown:00000000000000000000000000000000:1",  # unknown token
        "f:skip:notahex:1",  # bad uuid hex
        "f:skip:00000000000000000000000000000000:x",  # bad int
        "f:bogus:00000000000000000000000000000000:1",  # invalid priority
    ],
)
def test_malformed_form_callback_is_none(data: str) -> None:
    assert decode_form_callback(data) is None


def test_encoded_lengths_stay_within_limit() -> None:
    draft_id = uuid.UUID("1234567890abcdef1234567890abcdef")
    encodings = [
        encode_ticket_callback(TicketAction.to_waiting, 999999999, 999999),
        encode_form_callback("priority", draft_id, 999999, Priority.critical),
        encode_form_callback("skip", draft_id, 999999),
        encode_form_callback("ok", draft_id, 999999),
        encode_form_callback("no", draft_id, 999999),
    ]
    for data in encodings:
        assert len(data.encode("utf-8")) <= MAX_CALLBACK_DATA
