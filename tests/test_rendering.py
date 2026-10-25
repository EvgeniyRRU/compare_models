import uuid
from datetime import UTC, datetime

import pytest

from support_bot.callbacks import (
    MAX_CALLBACK_BYTES,
    CallbackDataError,
    DraftAction,
    DraftCallback,
    TicketCallback,
    parse_draft_callback,
    parse_ticket_callback,
)
from support_bot.domain.enums import Priority, Role, TicketStatus
from support_bot.domain.state_machine import Action
from support_bot.rendering import TELEGRAM_TEXT_LIMIT, render_post, truncate_escaped
from support_bot.services.views import TicketView, UserInfo

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
LONG_USER = UserInfo(1, 2**40, "u" * 32, "Я" * 64, "Ф" * 64, Role.SUPPORT)


def view(description: str, status: TicketStatus = TicketStatus.NEW) -> TicketView:
    return TicketView(
        id=2**62,
        status=status,
        priority=Priority.CRITICAL,
        description=description,
        request_id="<&>" * 21,
        tenant_id="&" * 64,
        author=LONG_USER,
        assignee=LONG_USER if status is not TicketStatus.NEW else None,
        version=2**31 - 1,
        created_at=NOW,
        updated_at=NOW,
        taken_at=None,
        closed_at=NOW if status is TicketStatus.RESOLVED else None,
        next_reminder_at=None,
        message_id=None,
        post_synced_version=0,
    )


@pytest.mark.parametrize("description", ["<&>" * 1000, "а" * 3000, "&" * 3000, "x"])
@pytest.mark.parametrize(
    "status", [TicketStatus.NEW, TicketStatus.IN_PROGRESS, TicketStatus.RESOLVED]
)
def test_post_fits_telegram_limit(description: str, status: TicketStatus) -> None:
    post = render_post(view(description, status))
    assert len(post.text) <= TELEGRAM_TEXT_LIMIT
    for row in post.keyboard:
        for button in row:
            assert len(button.callback_data.encode()) <= MAX_CALLBACK_BYTES


def test_long_description_truncated_with_ellipsis() -> None:
    post = render_post(view("&" * 3000), limit=1000)
    assert len(post.text) <= 1000 and post.text.endswith("…")
    assert "&amp;" in post.text and not post.text.endswith("&amp…")


def test_truncate_does_not_cut_entities() -> None:
    out = truncate_escaped("a&b&c&d", 10)
    assert len(out) <= 10 and out.endswith("…")
    assert out.count("&") == out.count("&amp;")


def test_buttons_by_status() -> None:
    new = render_post(view("x"))
    assert [b.text for row in new.keyboard for b in row] == ["✋ Взять"]
    taken = render_post(view("x", TicketStatus.IN_PROGRESS))
    actions = [parse_ticket_callback(b.callback_data).action for r in taken.keyboard for b in r]
    assert actions == [
        Action.TO_WAITING,
        Action.RESOLVE,
        Action.REJECT,
        Action.DUPLICATE,
        Action.RELEASE,
    ]
    assert render_post(view("x", TicketStatus.RESOLVED)).keyboard == ()


def test_ticket_callback_roundtrip() -> None:
    cb = TicketCallback(Action.TAKE, 123, 5)
    assert cb.encode() == "t:tk:123:5"
    assert parse_ticket_callback(cb.encode()) == cb


@pytest.mark.parametrize("action", list(DraftAction))
def test_draft_callback_roundtrip(action: DraftAction) -> None:
    prio = Priority.HIGH if action is DraftAction.PRIORITY else None
    cb = DraftCallback(action, uuid.uuid4(), 2**31 - 1, prio)
    data = cb.encode()
    assert len(data.encode()) <= MAX_CALLBACK_BYTES
    assert parse_draft_callback(data) == cb


@pytest.mark.parametrize(
    "data", ["t:xx:1:1", "t:tk:a:1", "t:tk:1", "d:p:short:1:h", "d:zz:x:1", "t:tk:-1:1"]
)
def test_bad_callbacks(data: str) -> None:
    parse = parse_ticket_callback if data.startswith("t") else parse_draft_callback
    with pytest.raises(CallbackDataError):
        parse(data)
