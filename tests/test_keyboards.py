"""Tests for inline keyboard builders (in-memory models, no database)."""

import uuid
from typing import Any

import pytest

from app.bot.keyboards import (
    TICKET_ACTION_LABELS,
    confirm_keyboard,
    priority_keyboard,
    tenant_id_keyboard,
    ticket_action_keyboard,
)
from app.db.enums import Priority, TicketStatus
from app.db.models import Ticket, TicketDraft
from app.services.render import PRIORITY_LABELS
from app.services.state_machine import TicketAction


def make_ticket(**kwargs: Any) -> Ticket:
    fields: dict[str, Any] = {"id": 42, "version": 3, "status": TicketStatus.new}
    fields.update(kwargs)
    return Ticket(**fields)


def make_draft(**kwargs: Any) -> TicketDraft:
    fields: dict[str, Any] = {
        "id": uuid.UUID("1234567890abcdef1234567890abcdef"),
        "version": 5,
    }
    fields.update(kwargs)
    return TicketDraft(**fields)


def _buttons(markup: Any) -> list[Any]:
    return [button for row in markup.inline_keyboard for button in row]


def test_new_ticket_keyboard_has_take_only() -> None:
    ticket = make_ticket(status=TicketStatus.new)

    kb = ticket_action_keyboard(ticket)
    assert kb is not None
    buttons = _buttons(kb)

    assert len(buttons) == 1
    assert buttons[0].text == TICKET_ACTION_LABELS[TicketAction.take]
    assert buttons[0].callback_data == f"t:t:{ticket.id}:{ticket.version}"


def test_in_progress_keyboard_has_five_buttons() -> None:
    ticket = make_ticket(status=TicketStatus.in_progress)

    kb = ticket_action_keyboard(ticket)
    assert kb is not None
    buttons = _buttons(kb)

    expected = {
        TICKET_ACTION_LABELS[TicketAction.to_waiting]: "w",
        TICKET_ACTION_LABELS[TicketAction.resolve]: "r",
        TICKET_ACTION_LABELS[TicketAction.reject]: "j",
        TICKET_ACTION_LABELS[TicketAction.duplicate]: "d",
        TICKET_ACTION_LABELS[TicketAction.abandon]: "a",
    }
    assert len(buttons) == 5
    for button in buttons:
        code = expected[button.text]
        assert button.callback_data == f"t:{code}:{ticket.id}:{ticket.version}"


def test_waiting_info_keyboard_has_five_buttons() -> None:
    ticket = make_ticket(status=TicketStatus.waiting_info)

    kb = ticket_action_keyboard(ticket)
    assert kb is not None
    buttons = _buttons(kb)

    texts = [button.text for button in buttons]
    assert texts == [
        TICKET_ACTION_LABELS[TicketAction.to_progress],
        TICKET_ACTION_LABELS[TicketAction.resolve],
        TICKET_ACTION_LABELS[TicketAction.reject],
        TICKET_ACTION_LABELS[TicketAction.duplicate],
        TICKET_ACTION_LABELS[TicketAction.abandon],
    ]
    assert buttons[0].callback_data == f"t:p:{ticket.id}:{ticket.version}"


@pytest.mark.parametrize(
    "status",
    [TicketStatus.resolved, TicketStatus.rejected, TicketStatus.duplicate],
)
def test_final_status_keyboard_is_none(status: TicketStatus) -> None:
    ticket = make_ticket(status=status)
    assert ticket_action_keyboard(ticket) is None


def test_priority_keyboard() -> None:
    draft = make_draft()

    kb = priority_keyboard(draft)
    buttons = _buttons(kb)

    assert [button.text for button in buttons] == [PRIORITY_LABELS[p] for p in Priority]
    for priority, button in zip(Priority, buttons, strict=True):
        assert button.callback_data == f"f:{priority.value}:{draft.id.hex}:{draft.version}"


def test_confirm_keyboard() -> None:
    draft = make_draft()

    kb = confirm_keyboard(draft)
    buttons = _buttons(kb)

    assert [button.text for button in buttons] == ["Отправить", "Отменить"]
    assert buttons[0].callback_data == f"f:ok:{draft.id.hex}:{draft.version}"
    assert buttons[1].callback_data == f"f:no:{draft.id.hex}:{draft.version}"


def test_tenant_id_keyboard() -> None:
    draft = make_draft()

    kb = tenant_id_keyboard(draft)
    buttons = _buttons(kb)

    assert len(buttons) == 1
    assert buttons[0].text == "Пропустить"
    assert buttons[0].callback_data == f"f:skip:{draft.id.hex}:{draft.version}"
