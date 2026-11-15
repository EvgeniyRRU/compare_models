"""Pure rendering tests: post text, truncation, keyboards. No DB needed."""

from telegram import InlineKeyboardMarkup

from app.bot.render import (
    MAX_POST_LEN,
    build_markup,
    callback_data,
    format_user,
    render_post,
)
from app.db.models import Priority, Ticket, TicketStatus, User
from app.domain.state_machine import Action


def ticket(status: TicketStatus = TicketStatus.in_progress, **kw: object) -> Ticket:
    defaults: dict = {
        "id": 7,
        "author_id": 1,
        "assignee_id": 2,
        "description": "нет горячей воды",
        "priority": Priority.critical,
        "request_id": "R-123",
        "tenant_id": "tenant-9",
        "status": status,
        "version": 3,
    }
    defaults.update(kw)
    return Ticket(**defaults)


AUTHOR = User(telegram_id=1, username="author", full_name="Автор Авторов")
DEV = User(telegram_id=2, username="dev", full_name="Девелопер")


def test_render_contains_all_fields() -> None:
    text = render_post(ticket(), AUTHOR, DEV)
    assert "№ 7" in text
    assert "В работе" in text
    assert "Приоритет: Критический" in text
    assert "Заявка: R-123" in text
    assert "Арендатор: tenant-9" in text
    assert "Автор: @author" in text
    assert "Исполнитель: @dev" in text
    assert "нет горячей воды" in text


def test_render_no_assignee_and_no_tenant() -> None:
    text = render_post(ticket(TicketStatus.new, assignee_id=None, tenant_id=None), AUTHOR, None)
    assert "Новое" in text
    assert "Арендатор: —" in text
    assert "Исполнитель: —" in text


def test_format_user_fallbacks() -> None:
    assert format_user(None) == "—"
    assert format_user(User(telegram_id=5, username=None, full_name="Иван")) == "Иван"
    assert format_user(User(telegram_id=5, username=None, full_name="")) == "id5"


def test_long_unicode_description_truncated_to_limit() -> None:
    desc = "П" * 3000 + "🔥" * 1500
    text = render_post(ticket(description=desc), AUTHOR, DEV)
    assert len(text) <= MAX_POST_LEN
    assert text.endswith("…")
    assert "№ 7" in text


def test_description_just_over_limit_gets_ellipsis() -> None:
    # One over the remaining budget must produce exactly MAX_POST_LEN chars.
    head_len = len(render_post(ticket(description=""), AUTHOR, DEV)) - len("")
    desc = "a" * (MAX_POST_LEN - head_len + 1)
    text = render_post(ticket(description=desc), AUTHOR, DEV)
    assert len(text) == MAX_POST_LEN
    assert text.endswith("…")


def test_short_description_not_truncated() -> None:
    text = render_post(ticket(description="короткое"), AUTHOR, DEV)
    assert "…" not in text
    assert text.endswith("короткое")


def _markup(markup: InlineKeyboardMarkup | None) -> list[list[tuple[str, str]]]:
    assert markup is not None
    return [[(b.text, b.callback_data or "") for b in row] for row in markup.inline_keyboard]


def test_markup_new_single_take_button() -> None:
    rows = _markup(build_markup(ticket(TicketStatus.new)))
    assert rows == [[("Взять", "t:take:7:3")]]


def test_markup_in_progress_rows() -> None:
    rows = _markup(build_markup(ticket(TicketStatus.in_progress)))
    assert rows[0] == [("Ожидание", "t:to_waiting:7:3")]
    assert [t for t, _ in rows[1]] == ["Решено", "Отклонено", "Дубль"]
    assert rows[2] == [("Отказаться", "t:abandon:7:3")]


def test_markup_waiting_moves_to_progress() -> None:
    rows = _markup(build_markup(ticket(TicketStatus.waiting_info)))
    assert rows[0] == [("В работу", "t:to_progress:7:3")]


def test_markup_final_status_no_buttons() -> None:
    for status in (
        TicketStatus.resolved,
        TicketStatus.rejected,
        TicketStatus.duplicate,
    ):
        assert build_markup(ticket(status)) is None


def test_callback_data_format() -> None:
    assert callback_data(Action.take, 42, 5) == "t:take:42:5"
    assert len(callback_data(Action.reassign, 10**15, 10**6)) <= 64
