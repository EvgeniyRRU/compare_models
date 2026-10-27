from uuid import uuid4

from telegram import ForceReply

from support_bot.db.models import Ticket, TicketDraft, User
from support_bot.telegram.rendering import (
    draft_keyboard,
    draft_prompt,
    render_post,
    ticket_keyboard,
)


def test_render_caps_astral_unicode_and_preserves_fields():
    author = User(id=1, telegram_id=12, first_name="А" * 255, last_name="Б" * 255, username="user")
    ticket = Ticket(
        id=123,
        description="😀" * 3000,
        priority="critical",
        request_id="R" * 64,
        tenant_id="T" * 64,
        status="new",
        version=1,
    )
    rendered = render_post(ticket, author, None)
    assert len(rendered.encode("utf-16-le")) // 2 <= 4096
    assert "…" in rendered
    assert "#123" in rendered and "R" * 64 in rendered and "T" * 64 in rendered
    assert "Новый" in rendered and "Не назначен" in rendered and "@user" in rendered


def test_status_keyboards_and_compact_draft_callbacks():
    ticket = Ticket(id=9223372036854775807, version=2147483647, status="in_progress")
    keyboard = ticket_keyboard(ticket)
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert f"t:wait:{ticket.id}:{ticket.version}" in callbacks
    assert all(len(value.encode()) <= 64 for value in callbacks)
    draft = TicketDraft(id=uuid4(), version=2147483647, step="priority")
    _, keyboard = draft_prompt(draft)
    assert not isinstance(keyboard, ForceReply)
    assert all(
        len(button.callback_data.encode()) <= 64
        for row in keyboard.inline_keyboard
        for button in row
    )
    draft.step = "tenant_id"
    assert isinstance(draft_prompt(draft)[1], ForceReply)
    assert draft_keyboard(draft, ["skip"]).inline_keyboard[0][0].callback_data.endswith(":skip")
