import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from app.db.models import Ticket, TicketDraft
from app.services.drafts import ActionResult, DraftService, Step, TextResult, decode_id, encode_id


@pytest.fixture
def ds(svc):
    return DraftService(svc, timedelta(hours=24))


async def start(sm, ds, uid):
    async with sm() as s, s.begin():
        return await ds.start(s, uid)


async def ask(sm, ds, d, msg_id):
    """Бот отправил вопрос шага и сохранил message_id."""
    async with sm() as s, s.begin():
        return await ds.set_prompt_message_id(s, d.id, d.version, d.step, msg_id)


async def text(sm, ds, uid, value, reply_to):
    async with sm() as s, s.begin():
        return await ds.submit_text(s, uid, value, reply_to)


async def get(sm, ds, uid):
    async with sm() as s:
        return await ds.get_active(s, uid)


async def press(sm, ds, uid, d, action, value=None, version=None):
    async with sm() as s, s.begin():
        return await ds.press(s, uid, d.id, d.version if version is None else version, action, value)


async def fill_to_confirm(sm, ds, uid):
    d = await start(sm, ds, uid)
    await ask(sm, ds, d, 10)
    _, d, _ = await text(sm, ds, uid, "описание", 10)
    _, d, _ = await press(sm, ds, uid, d, "p", "high")
    await ask(sm, ds, d, 11)
    _, d, _ = await text(sm, ds, uid, "REQ-1", 11)
    await ask(sm, ds, d, 12)
    _, d, _ = await text(sm, ds, uid, "T-1", 12)
    return d


def test_callback_data_fits_64_bytes():
    d = uuid.uuid4()
    assert decode_id(encode_id(d)) == d
    assert len(f"d:p:{encode_id(d)}:99999:critical".encode()) <= 64


async def test_full_flow_creates_ticket_and_deletes_draft(sm, ds, users):
    uid = users["support"].id
    d = await fill_to_confirm(sm, ds, uid)
    assert d.step == Step.CONFIRM
    res, _, ticket = await press(sm, ds, uid, d, "ok")
    assert res == ActionResult.SUBMITTED and ticket.description == "описание"
    assert (ticket.priority, ticket.request_id, ticket.tenant_id) == ("high", "REQ-1", "T-1")
    assert await get(sm, ds, uid) is None
    # повторное нажатие ничего не создаст
    res, _, again = await press(sm, ds, uid, d, "ok")
    assert res == ActionResult.STALE and again is None
    async with sm() as s:
        assert (await s.execute(select(func.count()).select_from(Ticket))).scalar_one() == 1


async def test_skip_tenant(sm, ds, users):
    uid = users["support"].id
    d = await start(sm, ds, uid)
    await ask(sm, ds, d, 10)
    _, d, _ = await text(sm, ds, uid, "x", 10)
    _, d, _ = await press(sm, ds, uid, d, "p", "low")
    await ask(sm, ds, d, 11)
    _, d, _ = await text(sm, ds, uid, "R", 11)
    res, d, _ = await press(sm, ds, uid, d, "s")
    assert d.step == Step.CONFIRM and "tenant_id" not in d.data
    _, _, ticket = await press(sm, ds, uid, d, "ok")
    assert ticket.tenant_id is None


async def test_validation_does_not_change_state(sm, ds, users):
    uid = users["support"].id
    d = await start(sm, ds, uid)
    await ask(sm, ds, d, 10)
    for bad in ("   ", "x" * 3001):
        res, view, err = await text(sm, ds, uid, bad, 10)
        assert res == TextResult.INVALID and err
        assert (view.version, view.step) == (d.version, d.step)
    assert (await get(sm, ds, uid)).version == d.version


async def test_text_not_a_reply_changes_nothing(sm, ds, users):
    uid = users["support"].id
    d = await start(sm, ds, uid)
    for reply_to in (None, 999):
        res, view, _ = await text(sm, ds, uid, "ответ", reply_to)
        assert res == TextResult.UNBOUND
    await ask(sm, ds, d, 10)
    res, _, _ = await text(sm, ds, uid, "ответ", 999)
    assert res == TextResult.UNBOUND
    cur = await get(sm, ds, uid)
    assert (cur.step, cur.version, cur.data) == (Step.DESCRIPTION, 1, {})


async def test_resent_answer_does_not_land_in_next_field(sm, ds, users):
    uid = users["support"].id
    d = await start(sm, ds, uid)
    await ask(sm, ds, d, 10)
    _, d, _ = await text(sm, ds, uid, "описание", 10)
    _, d, _ = await press(sm, ds, uid, d, "p", "high")
    await ask(sm, ds, d, 11)
    # ответ на вопрос 1 повторно после сбоя: reply на старый вопрос
    res, _, _ = await text(sm, ds, uid, "описание", 10)
    assert res == TextResult.UNBOUND
    cur = await get(sm, ds, uid)
    assert cur.step == Step.REQUEST_ID and "request_id" not in cur.data


async def test_prompt_reset_after_advance(sm, ds, users):
    uid = users["support"].id
    d = await start(sm, ds, uid)
    await ask(sm, ds, d, 10)
    _, d, _ = await text(sm, ds, uid, "описание", 10)
    assert d.prompt_message_id is None and d.version == 2
    # повтор того же ответа: prompt обнулён, ответ не принят
    res, _, _ = await text(sm, ds, uid, "описание", 10)
    assert res == TextResult.UNBOUND


async def test_late_prompt_save_does_not_overwrite_next_step(sm, ds, users):
    uid = users["support"].id
    d1 = await start(sm, ds, uid)  # шаг DESCRIPTION, v1
    await ask(sm, ds, d1, 10)
    _, d2, _ = await text(sm, ds, uid, "описание", 10)  # теперь PRIORITY, v2
    _, d3, _ = await press(sm, ds, uid, d2, "p", "high")  # REQUEST_ID, v3
    assert await ask(sm, ds, d3, 33) is True
    # запоздавшая отправка вопроса шага 1 пытается сохранить свой message_id
    assert await ask(sm, ds, d1, 99) is False
    assert (await get(sm, ds, uid)).prompt_message_id == 33
    # prompt_message_id не меняет version
    assert (await get(sm, ds, uid)).version == d3.version


async def test_buttons_from_deleted_draft_rejected(sm, ds, users):
    uid = users["support"].id
    old = await fill_to_confirm(sm, ds, uid)
    new = await start(sm, ds, uid)  # /new -> «Начать заново»
    res, _, ticket = await press(sm, ds, uid, old, "ok")
    assert res == ActionResult.STALE and ticket is None
    cur = await get(sm, ds, uid)
    assert (cur.id, cur.version, cur.step) == (new.id, 1, Step.DESCRIPTION)


async def test_stale_version_button_rejected(sm, ds, users):
    uid = users["support"].id
    d = await start(sm, ds, uid)
    await ask(sm, ds, d, 10)
    _, d2, _ = await text(sm, ds, uid, "описание", 10)
    res, _, _ = await press(sm, ds, uid, d2, "p", "high", version=d2.version - 1)
    assert res == ActionResult.STALE
    assert (await get(sm, ds, uid)).version == d2.version


async def test_wrong_step_button_rejected(sm, ds, users):
    uid = users["support"].id
    d = await start(sm, ds, uid)
    res, _, _ = await press(sm, ds, uid, d, "ok")
    assert res == ActionResult.STALE and (await get(sm, ds, uid)).version == 1


async def test_concurrent_answers_exactly_one_accepted(sm, ds, users):
    uid = users["support"].id
    d = await start(sm, ds, uid)
    await ask(sm, ds, d, 10)
    results = await asyncio.gather(*[text(sm, ds, uid, f"a{i}", 10) for i in range(5)])
    assert sorted(r[0] for r in results).count(TextResult.ACCEPTED) == 1
    cur = await get(sm, ds, uid)
    assert (cur.version, cur.step) == (2, Step.PRIORITY)


async def test_concurrent_double_submit_creates_one_ticket(sm, ds, users):
    uid = users["support"].id
    d = await fill_to_confirm(sm, ds, uid)
    results = await asyncio.gather(*[press(sm, ds, uid, d, "ok") for _ in range(4)])
    assert [r[0] for r in results].count(ActionResult.SUBMITTED) == 1
    async with sm() as s:
        assert (await s.execute(select(func.count()).select_from(Ticket))).scalar_one() == 1


async def test_one_draft_per_user_and_ttl(sm, ds, users):
    uid = users["support"].id
    a = await start(sm, ds, uid)
    b = await start(sm, ds, uid)
    assert a.id != b.id
    async with sm() as s, s.begin():
        await s.execute(
            update(TicketDraft).values(updated_at=func.now() - timedelta(hours=25))
        )
    async with sm() as s, s.begin():
        assert await ds.cleanup_expired(s) == 1
    assert await get(sm, ds, uid) is None
