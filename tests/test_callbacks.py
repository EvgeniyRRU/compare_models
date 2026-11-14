"""Tests for the callback-data codec (pure, no database/Telegram)."""

import uuid
from typing import Literal

import pytest

from app.bot.callbacks import (
    CODE_BY_TICKET_ACTION,
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
def test_form_simple_round_trip(action: Literal["skip", "ok", "no"]) -> None:
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


def test_encode_priority_requires_priority() -> None:
    draft_id = uuid.uuid4()
    with pytest.raises(ValueError):
        encode_form_callback("priority", draft_id, 1, None)


def test_encode_raises_when_over_limit() -> None:
    with pytest.raises(ValueError):
        encode_ticket_callback(TicketAction.take, ticket_id=1, version=10**60)


@pytest.mark.parametrize("token", ["+1", "-5", "4_2", " 42 ", "١", "１２３"])
def test_decode_rejects_non_strict_int_tokens(token: str) -> None:
    assert decode_ticket_callback(f"t:t:{token}:1") is None
    assert decode_ticket_callback(f"t:t:1:{token}") is None
    assert decode_form_callback(f"f:skip:00000000000000000000000000000000:{token}") is None


def test_code_maps_are_bijective() -> None:
    assert len(CODE_BY_TICKET_ACTION) == len(TICKET_ACTION_BY_CODE)
