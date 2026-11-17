# tests/integration/test_commands.py
from sqlalchemy import text
from telegram import Update

from tests.fakes import make_update


async def _u(tg_app, update_id, user_id, text=None, data=None, first_name="Test"):
    d = make_update(update_id, user_id, text=text, data=data)
    if first_name and "message" in d:
        d["message"]["from"]["first_name"] = first_name
    await tg_app.process_update(Update.de_json(d, tg_app.bot))


async def test_start_bootstrap_admin(tg_app, bot, db):
    await _u(tg_app, 1, 111, "/start")  # 111 — из BOOTSTRAP_ADMIN_IDS
    async with db.session_factory() as s:
        role = (
            await s.execute(text("SELECT role FROM users WHERE telegram_id = 111"))
        ).scalar_one()
    assert role == "admin"
    assert len(bot.sent) == 1


async def test_start_no_role_notification(tg_app, bot, db):
    await _u(tg_app, 2, 222, "/start")
    async with db.session_factory() as s:
        role = (
            await s.execute(text("SELECT role FROM users WHERE telegram_id = 222"))
        ).scalar_one()
    assert role is None
    assert "администратор" in bot.sent[-1]["text"].lower()


async def test_name_updated_on_every_interaction(tg_app, db):
    await _u(tg_app, 3, 333, "/start", first_name="Old")
    await _u(tg_app, 4, 333, "/help", first_name="New")
    async with db.session_factory() as s:
        name = (
            await s.execute(text("SELECT first_name FROM users WHERE telegram_id = 333"))
        ).scalar_one()
    assert name == "New"


async def test_help(tg_app, bot):
    await _u(tg_app, 5, 333, "/help")
    assert "/new" in bot.sent[-1]["text"]


async def test_cancel_without_draft(tg_app, bot):
    await _u(tg_app, 6, 333, "/start")
    await _u(tg_app, 7, 333, "/cancel")
    assert "Нет активной формы" in bot.sent[-1]["text"]


async def test_duplicate_update_processed_once(tg_app, bot, db):
    d = make_update(8, 444, text="/start")
    await tg_app.process_update(Update.de_json(d, tg_app.bot))
    sent_after_first = len(bot.sent)
    await tg_app.process_update(Update.de_json(d, tg_app.bot))
    assert len(bot.sent) == sent_after_first  # второй раз не обработан
