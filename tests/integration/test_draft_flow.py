# tests/integration/test_draft_flow.py
from sqlalchemy import text
from telegram import Update

from tests.fakes import make_update


async def _u(tg_app, update_id, user_id, text=None, data=None, message_id=1, reply_to=None):
    await tg_app.process_update(
        Update.de_json(
            make_update(
                update_id,
                user_id,
                text=text,
                data=data,
                message_id=message_id,
                reply_to_message_id=reply_to,
            ),
            tg_app.bot,
        )
    )


async def _draft_row(db, telegram_id):
    async with db.session_factory() as s:
        user_id = (
            await s.execute(text("SELECT id FROM users WHERE telegram_id = :t"), {"t": telegram_id})
        ).scalar_one()
        return (
            await s.execute(
                text(
                    "SELECT id, version, step, prompt_message_id "
                    "FROM ticket_drafts WHERE user_id = :u"
                ),
                {"u": user_id},
            )
        ).one_or_none()


async def _start_draft(tg_app, db, user_id):
    await _u(tg_app, 1, user_id, "/start")
    await _u(tg_app, 2, user_id, "/new")
    row = await _draft_row(db, user_id)
    assert row is not None and row.step == "description" and row.prompt_message_id is not None
    return row


def _cb(kind, draft_row, *extra):
    return ":".join(["d", kind, str(draft_row.id).replace("-", ""), str(draft_row.version), *extra])


async def test_full_flow_creates_ticket(tg_app, bot, db, settings):
    uid = 111  # bootstrap admin
    row = await _start_draft(tg_app, db, uid)
    # 1. описание
    await _u(tg_app, 3, uid, "проблема", message_id=10, reply_to=row.prompt_message_id)
    row = await _draft_row(db, uid)
    assert row.step == "priority"
    # 2. приоритет
    await _u(tg_app, 4, uid, data=_cb("prio", row, "h"))
    row = await _draft_row(db, uid)
    assert row.step == "request_id" and row.prompt_message_id is not None
    # 3. id заявки
    await _u(tg_app, 5, uid, "R-1", message_id=11, reply_to=row.prompt_message_id)
    row = await _draft_row(db, uid)
    assert row.step == "tenant_id"
    # 4. пропустить арендатора
    await _u(tg_app, 6, uid, data=_cb("skip", row))
    row = await _draft_row(db, uid)
    assert row.step == "confirm"
    # 5. отправить
    await _u(tg_app, 7, uid, data=_cb("send", row))
    async with db.session_factory() as s:
        t = (
            await s.execute(
                text("SELECT id, description, priority, request_id, tenant_id, status FROM tickets")
            )
        ).one()
        n_drafts = (await s.execute(text("SELECT count(*) FROM ticket_drafts"))).scalar_one()
    assert (t.description, t.priority, t.request_id, t.tenant_id, t.status) == (
        "проблема",
        "high",
        "R-1",
        None,
        "new",
    )
    assert n_drafts == 0
    # пост в чат разработчиков опубликован после коммита
    dev_posts = [m for m in bot.sent if m["chat_id"] == settings.dev_chat_id]
    assert len(dev_posts) == 1 and f"#{t.id}" in dev_posts[0]["text"]
    # подтверждение в личке
    dm = [m for m in bot.sent if m["chat_id"] == uid]
    assert any("создано" in m["text"] for m in dm)


async def test_text_without_draft_hint(tg_app, bot):
    await _u(tg_app, 1, 222, "/start")
    await _u(tg_app, 2, 222, "просто текст")
    assert any("new" in m["text"] for m in bot.sent if m["chat_id"] == 222)


async def test_unbound_message_reasks_no_change(tg_app, bot, db):
    uid = 111  # bootstrap admin (conftest: bootstrap_admin_ids=[111])
    await _start_draft(tg_app, db, uid)
    await _u(tg_app, 3, uid, "x", message_id=10, reply_to=999)  # reply на чужой вопрос
    row2 = await _draft_row(db, uid)
    assert row2.version == 1 and row2.step == "description"
    # вопрос задан повторно (новое сообщение)
    assert bot.sent[-1]["chat_id"] == uid


async def test_old_button_cannot_confirm_new_draft(tg_app, bot, db):
    """Кнопка «Отправить» удалённого черновика не подтверждает новый."""
    uid = 111  # bootstrap admin (conftest: bootstrap_admin_ids=[111])
    row = await _start_draft(tg_app, db, uid)
    # заполнить до confirm
    await _u(tg_app, 3, uid, "текст", message_id=10, reply_to=row.prompt_message_id)
    row = await _draft_row(db, uid)
    await _u(tg_app, 4, uid, data=_cb("prio", row, "c"))
    row = await _draft_row(db, uid)
    await _u(tg_app, 5, uid, "R-9", message_id=11, reply_to=row.prompt_message_id)
    row = await _draft_row(db, uid)
    await _u(tg_app, 6, uid, data=_cb("skip", row))
    old_row = await _draft_row(db, uid)
    old_send = _cb("send", old_row)
    # «Начать заново»
    await _u(tg_app, 7, uid, data=_cb("reset", old_row))
    new_row = await _draft_row(db, uid)
    assert new_row.id != old_row.id
    # нажатие старой кнопки send (uuid старого черновика)
    await _u(tg_app, 8, uid, data=old_send)
    async with db.session_factory() as s:
        n = (await s.execute(text("SELECT count(*) FROM tickets"))).scalar_one()
    assert n == 0  # ничего не создано
    assert bot.answered[-1]["show_alert"] is True
    assert (await _draft_row(db, uid)).version == 1  # новый черновик не тронут


async def test_new_without_role_forbidden(tg_app, bot, db):
    uid = 555
    await _u(tg_app, 1, uid, "/start")
    await _u(tg_app, 2, uid, "/new")
    assert (await _draft_row(db, uid)) is None
    assert "support" in bot.sent[-1]["text"]


async def test_new_with_existing_draft_offers_continue(tg_app, bot, db):
    uid = 111
    row = await _start_draft(tg_app, db, uid)
    await _u(tg_app, 3, uid, "текст", message_id=10, reply_to=row.prompt_message_id)
    await _u(tg_app, 4, uid, "/new")
    sent = bot.sent[-1]
    buttons = sent["reply_markup"].inline_keyboard[0]
    assert [b.text for b in buttons] == ["Продолжить", "Начать заново"]
