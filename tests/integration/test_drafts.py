# tests/integration/test_drafts.py
import asyncio

import pytest
from sqlalchemy import text

from app.services import drafts
from app.services import users as users_svc
from app.services.errors import (
    DraftValidationError,
    StaleAnswerError,
    StaleDraftError,
)


async def _make_user(db, tg: int = 1) -> int:
    async with db.session_factory() as s, s.begin():
        u = await users_svc.upsert_user(s, telegram_id=tg)
        return u.id


async def _draft(db, user_id):
    from sqlalchemy import select

    from app.models import Draft

    async with db.session_factory() as s:
        return (await s.execute(select(Draft).where(Draft.user_id == user_id))).scalar_one_or_none()


async def _fill_description(db, uid, draft_id, version, value="описание"):
    await drafts.save_prompt_message_id(db.session_factory, draft_id, version, "description", 100)
    async with db.session_factory() as s, s.begin():
        return await drafts.answer_text(s, uid, value, 100)


async def test_start_and_continue(db):
    uid = await _make_user(db)
    async with db.session_factory() as s, s.begin():
        d1, c1 = await drafts.start_draft(s, uid)
        d2, c2 = await drafts.start_draft(s, uid)
    assert c1 is True and c2 is False
    assert d1.id == d2.id


async def test_reset_replaces_draft(db):
    uid = await _make_user(db)
    async with db.session_factory() as s, s.begin():
        d1, _ = await drafts.start_draft(s, uid)
    async with db.session_factory() as s, s.begin():
        d2 = await drafts.reset_draft(s, uid, d1.id, d1.version)
    assert d2.id != d1.id and d2.version == 1
    async with db.session_factory() as s:
        n = (await s.execute(text("SELECT count(*) FROM ticket_drafts"))).scalar_one()
    assert n == 1


async def test_answer_text_advances(db):
    uid = await _make_user(db)
    async with db.session_factory() as s, s.begin():
        d, _ = await drafts.start_draft(s, uid)
    r = await _fill_description(db, uid, d.id, 1, "проблема")
    assert (r.step, r.version) == ("priority", 2)
    d = await _draft(db, uid)
    assert d.description == "проблема" and d.prompt_message_id is None


async def test_unbound_reply_changes_nothing(db):
    uid = await _make_user(db)
    async with db.session_factory() as s, s.begin():
        d, _ = await drafts.start_draft(s, uid)
    await drafts.save_prompt_message_id(db.session_factory, d.id, 1, "description", 100)
    for reply_to in (999, None):
        with pytest.raises(StaleAnswerError):
            async with db.session_factory() as s, s.begin():
                await drafts.answer_text(s, uid, "x", reply_to)
    d = await _draft(db, uid)
    assert (d.version, d.step, d.description) == (1, "description", None)


async def test_resent_answer_does_not_jump(db):
    """Повторно отправленный ответ не попадает в следующее поле."""
    uid = await _make_user(db)
    async with db.session_factory() as s, s.begin():
        d, _ = await drafts.start_draft(s, uid)
    await _fill_description(db, uid, d.id, 1, "первый")
    with pytest.raises(StaleAnswerError):
        async with db.session_factory() as s, s.begin():
            await drafts.answer_text(s, uid, "повтор", 100)
    d = await _draft(db, uid)
    assert (d.version, d.description) == (2, "первый")


async def test_validation(db):
    uid = await _make_user(db)
    async with db.session_factory() as s, s.begin():
        d, _ = await drafts.start_draft(s, uid)
    await drafts.save_prompt_message_id(db.session_factory, d.id, 1, "description", 100)
    for bad in ("", "   ", "x" * 3001):
        with pytest.raises(DraftValidationError):
            async with db.session_factory() as s, s.begin():
                await drafts.answer_text(s, uid, bad, 100)
    d = await _draft(db, uid)
    assert d.version == 1
    async with db.session_factory() as s, s.begin():
        await drafts.answer_text(s, uid, "x" * 3000, 100)
    assert (await _draft(db, uid)).version == 2


async def test_concurrent_answers_one_wins(db):
    uid = await _make_user(db)
    async with db.session_factory() as s, s.begin():
        d, _ = await drafts.start_draft(s, uid)
    await drafts.save_prompt_message_id(db.session_factory, d.id, 1, "description", 100)

    async def answer(value):
        async with db.session_factory() as s, s.begin():
            return await drafts.answer_text(s, uid, value, 100)

    results = await asyncio.gather(answer("A"), answer("B"), return_exceptions=True)
    ok = [r for r in results if not isinstance(r, Exception)]
    errs = [r for r in results if isinstance(r, Exception)]
    assert len(ok) == 1 and len(errs) == 1
    assert isinstance(errs[0], StaleAnswerError)
    d = await _draft(db, uid)
    assert d.version == 2 and d.description in ("A", "B")


async def test_stale_button_from_removed_draft(db):
    uid = await _make_user(db)
    async with db.session_factory() as s, s.begin():
        d, _ = await drafts.start_draft(s, uid)
        r = await drafts.answer_choice(s, None, uid, d.id, d.version, "canx")
    assert r.step is None and r.ticket_id is None
    with pytest.raises(StaleDraftError):
        async with db.session_factory() as s, s.begin():
            await drafts.answer_choice(s, None, uid, d.id, d.version, "send")
    async with db.session_factory() as s:
        n = (await s.execute(text("SELECT count(*) FROM tickets"))).scalar_one()
    assert n == 0


async def test_stale_version_rejected(db):
    uid = await _make_user(db)
    async with db.session_factory() as s, s.begin():
        d, _ = await drafts.start_draft(s, uid)
    with pytest.raises(StaleDraftError):
        async with db.session_factory() as s, s.begin():
            await drafts.answer_choice(s, None, uid, d.id, d.version + 5, "prio", "high")
    with pytest.raises(StaleDraftError):
        async with db.session_factory() as s, s.begin():
            await drafts.answer_choice(s, None, uid, d.id, d.version, "prio", "bogus")


async def test_late_question_does_not_overwrite_prompt(db):
    """Запоздавшая отправка вопроса не перезаписывает prompt_message_id следующего шага."""
    uid = await _make_user(db)
    async with db.session_factory() as s, s.begin():
        d, _ = await drafts.start_draft(s, uid)
    await drafts.save_prompt_message_id(db.session_factory, d.id, 1, "description", 100)
    r = await _fill_description(db, uid, d.id, 1, "описание")  # v2 priority
    async with db.session_factory() as s, s.begin():
        r = await drafts.answer_choice(s, None, uid, d.id, r.version, "prio", "high")  # v3
    await drafts.save_prompt_message_id(db.session_factory, d.id, r.version, "request_id", 102)
    # «запоздалый» вопрос description (v1) — не должен перезаписать
    await drafts.save_prompt_message_id(db.session_factory, d.id, 1, "description", 150)
    d = await _draft(db, uid)
    assert d.prompt_message_id == 102
    with pytest.raises(StaleAnswerError):
        async with db.session_factory() as s, s.begin():
            await drafts.answer_text(s, uid, "x", 150)


async def test_send_creates_ticket_and_deletes_draft(db, settings):
    uid = await _make_user(db)
    async with db.session_factory() as s, s.begin():
        d, _ = await drafts.start_draft(s, uid)
    r = await _fill_description(db, uid, d.id, 1, "описание")
    async with db.session_factory() as s, s.begin():
        r = await drafts.answer_choice(s, settings, uid, d.id, r.version, "prio", "critical")
    await drafts.save_prompt_message_id(db.session_factory, d.id, r.version, "request_id", 102)
    async with db.session_factory() as s, s.begin():
        r = await drafts.answer_text(s, uid, "R-42", 102)
    async with db.session_factory() as s, s.begin():
        r = await drafts.answer_choice(s, settings, uid, d.id, r.version, "skip")
    assert r.step == "confirm"
    async with db.session_factory() as s, s.begin():
        r = await drafts.answer_choice(s, settings, uid, d.id, r.version, "send")
    assert r.ticket_id is not None
    async with db.session_factory() as s:
        row = (
            await s.execute(
                text(
                    "SELECT description, priority, request_id, tenant_id, "
                    "status, version, author_id "
                    "FROM tickets WHERE id = :i"
                ),
                {"i": r.ticket_id},
            )
        ).one()
        n_drafts = (await s.execute(text("SELECT count(*) FROM ticket_drafts"))).scalar_one()
        ev = (
            await s.execute(
                text("SELECT event_type FROM ticket_events WHERE ticket_id = :i"),
                {"i": r.ticket_id},
            )
        ).scalar_one()
    assert row == ("описание", "critical", "R-42", None, "new", 1, uid)
    assert n_drafts == 0 and ev == "created"
    # повторное «Отправить» — ничего не создаст
    with pytest.raises(StaleDraftError):
        async with db.session_factory() as s, s.begin():
            await drafts.answer_choice(s, settings, uid, d.id, r.version, "send")
    async with db.session_factory() as s:
        n = (await s.execute(text("SELECT count(*) FROM tickets"))).scalar_one()
    assert n == 1


async def test_ttl_cleanup(db):
    uid = await _make_user(db)
    async with db.session_factory() as s, s.begin():
        await drafts.start_draft(s, uid)
    async with db.session_factory() as s, s.begin():
        await s.execute(text("UPDATE ticket_drafts SET updated_at = now() - interval '25 hours'"))
    assert await drafts.delete_expired_drafts(db.session_factory, 24) == 1
