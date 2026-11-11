from datetime import UTC, datetime

from support_bot.domain.state_machine import Action, Status
from support_bot.services.post_render import (
    TELEGRAM_LIMIT,
    PostView,
    actions_for,
    callback_data,
    parse_callback,
    render_post,
)


def _view(**overrides: object) -> PostView:
    base: dict[str, object] = {
        "ticket_id": 123,
        "version": 4,
        "status": Status.IN_PROGRESS,
        "priority": "high",
        "author": "Анна",
        "assignee": "Пётр",
        "request_id": "REQ-9",
        "tenant_id": "T-1",
        "description": "Не открывается отчёт",
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    base.update(overrides)
    return PostView(**base)  # type: ignore[arg-type]


def test_post_contains_key_fields() -> None:
    text = render_post(_view())

    assert "#123" in text
    assert "В работе" in text
    assert "Высокий" in text
    assert "Анна" in text
    assert "Пётр" in text
    assert "REQ-9" in text
    assert "T-1" in text
    assert "Не открывается отчёт" in text


def test_user_text_is_html_escaped() -> None:
    text = render_post(_view(description="<b>x</b> & y"))

    assert "&lt;b&gt;x&lt;/b&gt; &amp; y" in text


def test_long_description_is_cut_to_telegram_limit() -> None:
    text = render_post(_view(description="я" * 10_000))

    assert len(text) <= TELEGRAM_LIMIT
    assert "…\n\nПолный текст — в карточке Mini App" in text


def test_actions_follow_status() -> None:
    assert actions_for(Status.NEW) == [Action.TAKE]
    assert Action.RELEASE in actions_for(Status.IN_PROGRESS)
    assert Action.WAIT_INFO in actions_for(Status.IN_PROGRESS)
    assert Action.RESUME in actions_for(Status.WAITING_INFO)
    assert Action.RESUME not in actions_for(Status.IN_PROGRESS)
    assert actions_for(Status.RESOLVED) == []


def test_callback_data_is_compact_and_round_trips() -> None:
    data = callback_data(Action.RESOLVE, ticket_id=2_000_000_000, version=99_999)

    assert len(data.encode()) <= 64
    assert parse_callback(data) == (Action.RESOLVE, 2_000_000_000, 99_999)


def test_parse_rejects_foreign_callback() -> None:
    assert parse_callback("x:whatever") is None
