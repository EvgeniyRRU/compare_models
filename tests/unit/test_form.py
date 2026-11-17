import uuid as uuid_lib
from types import SimpleNamespace

from app.bot import form


def _draft(version=1, step="description", **kw):
    base = dict(
        id=uuid_lib.uuid4(),
        version=version,
        step=step,
        description="desc",
        priority="high",
        request_id="r1",
        tenant_id=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_callback_within_64_bytes():
    d = _draft(version=1234)
    for kind, extra in (
        ("send", None),
        ("canx", None),
        ("cont", None),
        ("reset", None),
        ("skip", None),
        ("prio", "c"),
    ):
        data = form.cb(kind, d, extra) if extra else form.cb(kind, d)
        assert len(data.encode()) <= 64, kind


def test_parse_roundtrip():
    d = _draft(version=7)
    kind, uid, version, extra = form.parse_draft_callback(form.cb("prio", d, "h"))
    assert (kind, uid, version, extra) == ("prio", str(d.id).replace("-", ""), 7, "h")


def test_parse_rejects_foreign():
    assert form.parse_draft_callback("t:take:1:2") is None
    assert form.parse_draft_callback("d:send:short:1") is None
    assert form.parse_draft_callback("garbage") is None
    assert form.parse_draft_callback("") is None


def test_questions():
    text, markup = form.question_for("description", _draft(step="description"))
    assert "3000" in text and markup is not None
    text2, markup2 = form.question_for("priority", _draft(step="priority"))
    assert "приоритет" in text2.lower()
    assert len(markup2.inline_keyboard) == 1 and len(markup2.inline_keyboard[0]) == 4
    text3, markup3 = form.question_for("confirm", _draft(step="confirm"))
    assert "desc" in text3 and "r1" in text3
