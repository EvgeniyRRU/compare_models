# tests/integration/test_post_callbacks.py
from sqlalchemy import text
from telegram import Update

from tests.fakes import make_update


async def _u(tg_app, update_id, user_id, data=None, text=None):
    await tg_app.process_update(
        Update.de_json(make_update(update_id, user_id, data=data, text=text), tg_app.bot)
    )


async def _ticket(db, settings, author_tg: int = 111) -> int:
    from app.services import tickets as tsvc
    from app.services import users as usvc

    async with db.session_factory() as s, s.begin():
        u = await usvc.upsert_user(s, telegram_id=author_tg, first_name="A")
        t = await tsvc.create_ticket(
            s, settings, u.id, description="d", priority="high", request_id="r"
        )
    return t.id


async def _dev_id(tg_app, db, tg: int = 222) -> int:
    from app.services import users as usvc

    await _u(tg_app, 50, tg, text="/start")
    async with db.session_factory() as s, s.begin():
        u = await usvc.upsert_user(s, telegram_id=tg)
        await usvc.change_role(s, user_id=u.id, new_role="developer")
        return u.id


def _t(action, tid, v):
    return f"t:{action}:{tid}:{v}"


async def test_take_via_button(tg_app, bot, db, settings):
    tid = await _ticket(db, settings)
    dev_id = await _dev_id(tg_app, db)
    await _u(tg_app, 100, 222, data=_t("take", tid, 1))
    async with db.session_factory() as s:
        row = (
            await s.execute(
                text("SELECT status, assignee_id FROM tickets WHERE id = :i"),
                {"i": tid},
            )
        ).one()
    assert row == ("in_progress", dev_id)
    dev_posts = [m for m in bot.sent if m["chat_id"] == settings.dev_chat_id]
    assert len(dev_posts) == 1
    assert "в работе" in dev_posts[0]["text"]


async def test_stale_take_alert(tg_app, bot, db, settings):
    tid = await _ticket(db, settings)
    await _dev_id(tg_app, db)
    await _u(tg_app, 101, 222, data=_t("take", tid, 1))
    await _u(tg_app, 102, 222, data=_t("take", tid, 1))  # старая кнопка
    last = bot.answered[-1]
    assert last["show_alert"] is True
    assert "взято" in last["text"]


async def test_permission_denied(tg_app, bot, db, settings):
    tid = await _ticket(db, settings)
    await _dev_id(tg_app, db)
    await _u(tg_app, 103, 222, data=_t("take", tid, 1))
    await _u(tg_app, 30, 333, text="/start")
    await _u(tg_app, 104, 333, data=_t("res", tid, 2))
    assert bot.answered[-1]["show_alert"] is True
    async with db.session_factory() as s:
        status = (
            await s.execute(text("SELECT status FROM tickets WHERE id = :i"), {"i": tid})
        ).scalar_one()
    assert status == "in_progress"


async def test_status_cycle_via_buttons(tg_app, bot, db, settings):
    tid = await _ticket(db, settings)
    await _dev_id(tg_app, db)
    for v, (action, _) in enumerate(
        (
            ("take", "in_progress"),
            ("wai", "waiting_info"),
            ("pro", "in_progress"),
            ("res", "resolved"),
        ),
        start=1,
    ):
        await _u(tg_app, 200 + v, 222, data=_t(action, tid, v))
    async with db.session_factory() as s:
        row = (
            await s.execute(
                text("SELECT status, closed_at IS NOT NULL, version FROM tickets WHERE id = :i"),
                {"i": tid},
            )
        ).one()
    assert row == ("resolved", True, 5)
    last_post = bot.edited[-1]["text"] if bot.edited else bot.sent[-1]["text"]
    assert "решено" in last_post
