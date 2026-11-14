"""Tests for the pure post-text renderer (no I/O)."""

from typing import Any

from app.db.enums import Priority, TicketStatus
from app.db.models import Ticket, User
from app.services.render import PRIORITY_LABELS, STATUS_LABELS, render_post_text, user_display_name


def make_user(**kwargs: Any) -> User:
    fields: dict[str, Any] = {"telegram_id": 1, "username": None, "full_name": None}
    fields.update(kwargs)
    return User(**fields)


def make_ticket(**kwargs: Any) -> Ticket:
    fields: dict[str, Any] = {
        "id": 42,
        "author_id": 1,
        "description": "сломался лифт",
        "priority": Priority.medium,
        "request_id": "REQ-99",
        "tenant_id": None,
        "status": TicketStatus.new,
        "version": 1,
    }
    fields.update(kwargs)
    return Ticket(**fields)


def test_render_post_text_includes_all_fields() -> None:
    author = make_user(full_name="Иван Иванов")
    assignee = make_user(telegram_id=2, username="dev_petrov")
    ticket = make_ticket(
        priority=Priority.critical,
        tenant_id="tenant-7",
        status=TicketStatus.in_progress,
        description="не работает оплата",
    )

    text = render_post_text(ticket, author=author, assignee=assignee)

    assert "#42" in text
    assert PRIORITY_LABELS[Priority.critical] in text
    assert "REQ-99" in text
    assert "tenant-7" in text
    assert "Иван Иванов" in text
    assert STATUS_LABELS[TicketStatus.in_progress] in text
    assert "dev_petrov" in text
    assert "не работает оплата" in text


def test_render_post_text_omits_optional_fields_when_unset() -> None:
    author = make_user(full_name="Автор")
    ticket = make_ticket(tenant_id=None, status=TicketStatus.new)

    text = render_post_text(ticket, author=author, assignee=None)

    assert "Автор" in text
    assert "Исполнитель" not in text
    assert "Арендатор" not in text


def test_long_description_is_truncated_within_limit() -> None:
    author = make_user(full_name="Автор")
    ticket = make_ticket(description="x" * 5000)

    text = render_post_text(ticket, author=author, assignee=None)

    assert len(text) <= 4096
    assert text.endswith("…")
    # The description itself was truncated (not passed through verbatim).
    assert "x" * 5000 not in text


def test_user_display_name_prefers_full_name() -> None:
    assert user_display_name(make_user(full_name="Иван", username="ivan")) == "Иван"


def test_user_display_name_falls_back_to_username() -> None:
    assert user_display_name(make_user(username="ivan")) == "ivan"


def test_user_display_name_falls_back_to_telegram_id() -> None:
    assert user_display_name(make_user(telegram_id=777)) == "777"


def test_user_display_name_none_is_dash() -> None:
    assert user_display_name(None) == "—"
