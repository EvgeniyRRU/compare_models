from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot.context import AppContext
from app.bot.handlers import ticket_callback
from app.config import Settings
from app.db.models import Ticket
from app.domain.state_machine import Priority
from app.services.drafts import DraftService
from app.services.post_sync import PostSyncer
from app.services.tickets import NewTicket
from tests.fakes import FakePublisher


@pytest.fixture
def ctx(engine, sm, svc):
    pub = FakePublisher()
    return AppContext(
        settings=Settings(bot_token="1:T", dev_chat_id=-1),
        engine=engine,
        sm=sm,
        tickets=svc,
        drafts=DraftService(svc, timedelta(hours=1)),
        syncer=PostSyncer(engine, sm, pub, -1, 3),
        publisher=pub,
    )


def click(ctx, tg_id: int, data: str):
    q = SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=tg_id, username=f"u{tg_id}", first_name="N", last_name=None),
        answer=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=q)
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"ctx": ctx}))
    return update, context, q


async def test_take_race_alerts_and_resyncs(sm, svc, users, ctx):
    async with sm() as s, s.begin():
        t = await svc.create(s, users["support"].id, NewTicket("d", Priority.HIGH, "R"))
    u, c, q1 = click(ctx, 2, f"t:tk:{t.id}:1")
    await ticket_callback(u, c)
    q1.answer.assert_awaited_once_with()  # успех, без alert
    u, c, q2 = click(ctx, 3, f"t:tk:{t.id}:1")
    await ticket_callback(u, c)
    q2.answer.assert_awaited_once_with("Уже взято", show_alert=True)
    assert (
        len(ctx.publisher.sent) == 1 and len(ctx.publisher.edits) == 1
    )  # пост пересинхронизирован


async def test_permission_denied_alert(sm, svc, users, ctx):
    async with sm() as s, s.begin():
        t = await svc.create(s, users["support"].id, NewTicket("d", Priority.HIGH, "R"))
    u, c, q = click(ctx, 1, f"t:tk:{t.id}:1")  # support не может брать
    await ticket_callback(u, c)
    assert q.answer.await_args.kwargs == {"show_alert": True}
    async with sm() as s:
        assert (await s.get(Ticket, t.id)).status == "new"


async def test_stale_alert_and_garbage(sm, svc, users, ctx):
    async with sm() as s, s.begin():
        t = await svc.create(s, users["support"].id, NewTicket("d", Priority.HIGH, "R"))
    u, c, q = click(ctx, 2, f"t:tk:{t.id}:7")
    await ticket_callback(u, c)
    q.answer.assert_awaited_once_with("Обращение изменилось", show_alert=True)
    u, c, q = click(ctx, 2, "t:zz:1:1")
    await ticket_callback(u, c)
    assert q.answer.await_args.kwargs == {"show_alert": True}
