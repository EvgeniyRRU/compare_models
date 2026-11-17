from types import SimpleNamespace

from app.services.post_sync import MAX_POST_LEN, format_user, render_post


def _ticket(**kw):
    base = dict(
        id=123,
        description="desc",
        priority="high",
        request_id="R-1",
        tenant_id=None,
        status="new",
        assignee_id=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _user(first="John", username="john", telegram_id=5):
    return SimpleNamespace(first_name=first, username=username, telegram_id=telegram_id)


def test_render_basic():
    text = render_post(_ticket(), _user(), None)
    assert "#123" in text
    assert "R-1" in text
    assert "John (@john)" in text
    assert "высокий" in text
    assert "новое" in text
    assert "Исполнитель: —" in text


def test_render_assignee_tenant_and_status():
    t = _ticket(tenant_id="T-9", status="in_progress")
    text = render_post(t, _user(), _user(first="Dev", username=None, telegram_id=7))
    assert "T-9" in text
    assert "Dev" in text
    assert "в работе" in text


def test_render_truncates_to_limit():
    t = _ticket(description="x" * 5000)
    text = render_post(t, _user(), None)
    assert len(text) <= MAX_POST_LEN
    assert text.count("…") == 1
    assert "x" * 5000 not in text


def test_render_does_not_mutate_ticket():
    t = _ticket(description="y" * 5000)
    original = t.description
    render_post(t, _user(), None)
    assert t.description == original


def test_format_user_variants():
    assert format_user(_user()) == "John (@john)"
    assert format_user(_user(first="Dev", username=None)) == "Dev"
    assert format_user(_user(first=None, username="nick")) == "@nick"
    assert format_user(_user(first=None, username=None, telegram_id=42)) == "ID 42"
