"""Напоминания: снятие срока, SKIP LOCKED, привязка к моменту снятия, отправка."""

import asyncio

from sqlalchemy import text

from app.config import settings
from app.db.models import Role, Ticket, TicketStatus, User
from app.services import tickets as tickets_svc
from app.services.reminders import claim_due, format_delay, reminder_text, sweep_once
from app.telegram_gateway import RetryAfter
from tests.fake_gateway import FakeGateway

SUPPORT_ID = 850
DEV_ID = 851


async def mk_user(session, telegram_id: int, role: Role | None) -> User:
    user = User(
        telegram_id=telegram_id,
        username=f"u{telegram_id}",
        full_name=f"User {telegram_id}",
        role=role,
    )
    session.add(user)
    await session.commit()
    return user


async def mk_ticket(session, priority: str = "critical") -> Ticket:
    return await tickets_svc.create(
        session,
        settings,
        author_id=SUPPORT_ID,
        description="Не работает холодная вода",
        priority=priority,
        request_id="REQ-7",
        tenant_id="T-3",
    )


async def backdate_due(session, ticket_id: int, minutes: int) -> None:
    """Отмотать срок напоминания и момент входа в new на N минут назад."""
    await session.execute(
        text(
            "UPDATE tickets SET next_reminder_at = now() - make_interval(mins => :m) "
            "WHERE id = :id"
        ),
        {"m": minutes, "id": ticket_id},
    )
    await session.execute(
        text(
            "UPDATE ticket_events SET created_at = now() - make_interval(mins => :m) "
            "WHERE ticket_id = :id AND event_type = 'created'"
        ),
        {"m": minutes, "id": ticket_id},
    )
    await session.commit()


async def next_reminder_window(session, ticket_id: int, minutes: int) -> bool:
    """next_reminder_at в пределах now()+minutes ± 2 минуты."""
    return bool(
        await session.scalar(
            text(
                "SELECT next_reminder_at >= now() + make_interval(mins => :lo) "
                "AND next_reminder_at <= now() + make_interval(mins => :hi) "
                "FROM tickets WHERE id = :id"
            ),
            {"lo": minutes - 2, "hi": minutes + 2, "id": ticket_id},
        )
    )


async def get_ticket(session, ticket_id: int) -> Ticket:
    fresh = await session.get(Ticket, ticket_id)
    await session.refresh(fresh)
    assert fresh is not None
    return fresh


def test_format_delay():
    assert format_delay(0) == "0 мин"
    assert format_delay(5) == "5 мин"
    assert format_delay(60) == "1 ч"
    assert format_delay(65) == "1 ч 5 мин"
    assert format_delay(125) == "2 ч 5 мин"
    assert format_delay(-10) == "0 мин"


def test_reminder_text():
    assert reminder_text(42, "critical", 35) == "⏰ #42 не взято 35 мин — приоритет Критический"


async def test_entry_to_new_arms_reminder(db_session):
    await mk_user(db_session, SUPPORT_ID, Role.support)
    await mk_user(db_session, DEV_ID, Role.developer)
    ticket = await mk_ticket(db_session, priority="high")
    assert ticket.next_reminder_at is not None
    assert await next_reminder_window(db_session, ticket.id, 30)


async def test_future_due_not_claimed(db_session, session_factory):
    await mk_user(db_session, SUPPORT_ID, Role.support)
    ticket = await mk_ticket(db_session)
    fake = FakeGateway()
    assert await sweep_once(fake, session_factory, settings) == 0
    assert fake.calls == []
    fresh = await get_ticket(db_session, ticket.id)
    assert fresh.next_reminder_at is not None


async def test_due_claim_sends_once_with_reply(db_session, session_factory):
    await mk_user(db_session, SUPPORT_ID, Role.support)
    ticket = await mk_ticket(db_session)
    await db_session.execute(
        text("UPDATE tickets SET message_id = 777 WHERE id = :id"), {"id": ticket.id}
    )
    await backdate_due(db_session, ticket.id, 35)

    fake = FakeGateway()
    assert await sweep_once(fake, session_factory, settings) == 1
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call.kind == "send"
    assert call.chat_id == settings.dev_chat_id
    assert call.reply_to == 777
    assert call.text == f"⏰ #{ticket.id} не взято 35 мин — приоритет Критический"

    # Второй заход — срок уже перенесён, дупликата нет.
    assert await sweep_once(fake, session_factory, settings) == 0
    assert len(fake.calls) == 1
    assert await next_reminder_window(db_session, ticket.id, 10)


async def test_no_post_sends_without_reply(db_session, session_factory):
    await mk_user(db_session, SUPPORT_ID, Role.support)
    ticket = await mk_ticket(db_session)
    assert ticket.message_id is None
    await backdate_due(db_session, ticket.id, 15)

    fake = FakeGateway()
    assert await sweep_once(fake, session_factory, settings) == 1
    assert fake.calls[0].reply_to is None


async def test_next_due_anchored_at_claim_not_stale(db_session, session_factory):
    """Просрочка на 3 часа -> новый срок = время снятия + интервал, без догона."""
    await mk_user(db_session, SUPPORT_ID, Role.support)
    ticket = await mk_ticket(db_session)  # critical: интервал 10 мин
    await backdate_due(db_session, ticket.id, 180)

    fake = FakeGateway()
    assert await sweep_once(fake, session_factory, settings) == 1
    # Не now()-3ч+10мин, а именно now()+10мин.
    assert await next_reminder_window(db_session, ticket.id, 10)
    assert await sweep_once(fake, session_factory, settings) == 0


async def test_taken_ticket_not_reminded(db_session, session_factory):
    await mk_user(db_session, SUPPORT_ID, Role.support)
    await mk_user(db_session, DEV_ID, Role.developer)
    ticket = await mk_ticket(db_session)
    await backdate_due(db_session, ticket.id, 60)
    await tickets_svc.take(
        db_session, settings, ticket_id=ticket.id, actor_id=DEV_ID, expected_version=1
    )
    await db_session.commit()
    fresh = await get_ticket(db_session, ticket.id)
    assert fresh.status == TicketStatus.in_progress
    assert fresh.next_reminder_at is None  # инвариант выхода из new

    fake = FakeGateway()
    assert await sweep_once(fake, session_factory, settings) == 0


async def test_send_error_is_not_retried(db_session, session_factory):
    await mk_user(db_session, SUPPORT_ID, Role.support)
    ticket = await mk_ticket(db_session)
    await backdate_due(db_session, ticket.id, 20)

    fake = FakeGateway()
    fake.script(RetryAfter(seconds=1))
    assert await sweep_once(fake, session_factory, settings) == 0
    assert fake.errors == []  # попытка ровно одна: scripted-ошибка израсходована
    assert await sweep_once(fake, session_factory, settings) == 0  # догона нет
    assert fake.calls == []


async def test_claim_not_blocked_by_other_holder(session_factory, db_session):
    """Чужая FOR UPDATE блокировка не останавливает claim: SKIP LOCKED пропускает строку."""
    await mk_user(db_session, SUPPORT_ID, Role.support)
    ticket = await mk_ticket(db_session)
    await backdate_due(db_session, ticket.id, 30)

    blocker = session_factory()
    try:
        tx = await blocker.begin()
        await blocker.execute(
            text("SELECT id FROM tickets WHERE id = :id FOR UPDATE"), {"id": ticket.id}
        )
        claimed = await asyncio.wait_for(claim_due(session_factory, settings), timeout=2)
        assert claimed == []
        await tx.commit()
    finally:
        await blocker.close()

    # После снятия чужой блокировки строка снова должная и берётся.
    fake = FakeGateway()
    assert await sweep_once(fake, session_factory, settings) == 1


async def test_two_workers_never_double_claim(db_session, session_factory):
    await mk_user(db_session, SUPPORT_ID, Role.support)
    ticket = await mk_ticket(db_session)
    await backdate_due(db_session, ticket.id, 15)

    fake = FakeGateway()
    results = await asyncio.gather(
        sweep_once(fake, session_factory, settings),
        sweep_once(fake, session_factory, settings),
    )
    assert sum(results) == 1
    assert len(fake.calls) == 1


async def test_claim_respects_priority_intervals(db_session, session_factory):
    await mk_user(db_session, SUPPORT_ID, Role.support)
    t_med = await mk_ticket(db_session, priority="medium")
    t_low = await mk_ticket(db_session, priority="low")
    await backdate_due(db_session, t_med.id, 200)
    await backdate_due(db_session, t_low.id, 600)

    claimed = await claim_due(session_factory, settings)
    assert {c.ticket_id for c in claimed} == {t_med.id, t_low.id}
    assert await next_reminder_window(db_session, t_med.id, 120)
    assert await next_reminder_window(db_session, t_low.id, 480)
