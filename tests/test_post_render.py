from types import SimpleNamespace

from app.bot.posts import render_ticket


def test_render_ticket_stays_within_telegram_message_limit() -> None:
    ticket = SimpleNamespace(
        id=123,
        description="x" * 3000,
        priority="high",
        request_id="REQ-1",
        tenant_id="TEN-1",
        status="new",
        version=1,
        author=SimpleNamespace(full_name="Support"),
        assignee=None,
    )
    assert len(render_ticket(ticket)) <= 4096
