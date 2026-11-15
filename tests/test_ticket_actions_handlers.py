"""Ticket action buttons over the handler core: versions, permissions, post sync."""

import asyncio

from sqlalchemy import select

from app.bot.handlers.common import Deps
from app.bot.handlers.router import route_callback
from app.bot.handlers.ticket_actions import handle_ticket_callback, parse_ticket_callback
from app.config import settings
from app.db.models import Role, Ticket, TicketStatus, User
from app.domain.state_machine import Action
from app.services import tickets as tickets_svc
from tests.fake_gateway import FakeGateway

SUPPORT = 810
DEV = 811
ADMIN = 812


def make_deps(session_factory):
    fake = FakeGateway()
    return Deps(gateway=fake, session_factory=session_factory, settings=settings), fake


async def mk_user(session_factory, telegram_id: int, role: Role | None) -> None:
    async with session_factory() as s, s.begin():
        s.add(
            User(
                telegram_id=telegram_id,
                username=f"a{telegram_id}",
                full_name=f"Action {telegram_id}",
                role=role,
            )
        )


async def mk_ticket(session_factory) -> Ticket:
    async with session_factory() as s, s.begin():
        return await tickets_svc.create(
            s,
            settings,
            author_id=SUPPORT,
            description="Протечка под раковиной",
            priority="critical",
            request_id="RQ-9",
            tenant_id="T-1",
        )


async def fetch(session_factory, ticket_id: int) -> Ticket:
    async with session_factory() as s:
        t = await s.get(Ticket, ticket_id)
    assert t is not None
    return t


async def press(
    deps, fake: FakeGateway, actor: int, action: str, ticket_id: int, version: int
) -> None:
    await handle_ticket_callback(
        deps,
        user_id=actor,
        callback_id=f"cb-{action}-{version}",
        ticket_id=ticket_id,
        action=Action(action),
        expected_version=version,
    )


async def test_take_by_developer_updates_ticket_and_post(session_factory) -> None:
    await mk_user(session_factory, SUPPORT, Role.support)
    await mk_user(session_factory, DEV, Role.developer)
    ticket = await mk_ticket(session_factory)
    deps, fake = make_deps(session_factory)

    await press(deps, fake, DEV, "take", ticket.id, 1)

    after = await fetch(session_factory, ticket.id)
    assert after.status is TicketStatus.in_progress and after.assignee_id == DEV
    assert after.version == 2
    assert fake.last_alert_text() == "Готово"
    posts = [c for c in fake.calls if c.chat_id == settings.dev_chat_id]
    assert posts and "В работе" in posts[-1].text and after.message_id == posts[-1].message_id


async def test_forbidden_actors_get_alerts_and_no_change(session_factory) -> None:
    await mk_user(session_factory, SUPPORT, Role.support)
    await mk_user(session_factory, DEV, Role.developer)
    await mk_user(session_factory, 813, None)
    ticket = await mk_ticket(session_factory)
    deps, fake = make_deps(session_factory)

    await press(deps, fake, 813, "take", ticket.id, 1)  # no role
    assert "доступ" in (fake.last_alert_text() or "").lower()
    await press(deps, fake, SUPPORT, "take", ticket.id, 1)  # support is not an executor
    assert fake.last_alert_text() not in (None, "Готово")

    after = await fetch(session_factory, ticket.id)
    assert after.status is TicketStatus.new and after.assignee_id is None and after.version == 1
    assert not [c for c in fake.calls if c.chat_id == settings.dev_chat_id]


async def test_stale_version_alerts(session_factory) -> None:
    await mk_user(session_factory, SUPPORT, Role.support)
    await mk_user(session_factory, DEV, Role.developer)
    ticket = await mk_ticket(session_factory)
    deps, fake = make_deps(session_factory)

    await press(deps, fake, DEV, "take", ticket.id, 1)
    assert fake.last_alert_text() == "Готово"

    await press(deps, fake, DEV, "take", ticket.id, 1)  # same button pressed again
    assert fake.last_alert_text() == "Уже взято"
    await press(deps, fake, DEV, "to_waiting", ticket.id, 1)  # stale v1 button
    assert fake.last_alert_text() == "Обращение изменилось"
    after = await fetch(session_factory, ticket.id)
    assert after.status is TicketStatus.in_progress  # unchanged by both stale presses


async def test_take_race_exactly_one_winner(session_factory) -> None:
    await mk_user(session_factory, SUPPORT, Role.support)
    await mk_user(session_factory, DEV, Role.developer)
    await mk_user(session_factory, 814, Role.developer)
    ticket = await mk_ticket(session_factory)
    deps, fake = make_deps(session_factory)

    await asyncio.gather(
        handle_ticket_callback(
            deps,
            user_id=DEV,
            callback_id="r1",
            ticket_id=ticket.id,
            action=Action.take,
            expected_version=1,
        ),
        handle_ticket_callback(
            deps,
            user_id=814,
            callback_id="r2",
            ticket_id=ticket.id,
            action=Action.take,
            expected_version=1,
        ),
    )
    alerts = sorted(a.text or "" for a in fake.alerts)
    assert alerts == ["Готово", "Уже взято"]
    after = await fetch(session_factory, ticket.id)
    assert after.status is TicketStatus.in_progress
    assert after.assignee_id in (DEV, 814)


async def test_close_and_abandon_flow(session_factory) -> None:
    await mk_user(session_factory, SUPPORT, Role.support)
    await mk_user(session_factory, DEV, Role.developer)
    await mk_user(session_factory, ADMIN, Role.admin)
    ticket = await mk_ticket(session_factory)
    deps, fake = make_deps(session_factory)

    await press(deps, fake, DEV, "take", ticket.id, 1)
    await press(deps, fake, DEV, "abandon", ticket.id, 2)
    after = await fetch(session_factory, ticket.id)
    assert after.status is TicketStatus.new and after.assignee_id is None
    assert after.next_reminder_at is not None  # re-armed for the new state
    assert "Отказаться" not in str([c.markup for c in fake.calls[-1:]])

    # Re-take with the current version.
    await press(deps, fake, ADMIN, "take", ticket.id, after.version)
    after = await fetch(session_factory, ticket.id)
    assert after.assignee_id == ADMIN
    # Resolve closes it.
    await press(deps, fake, ADMIN, "resolve", ticket.id, after.version)
    after = await fetch(session_factory, ticket.id)
    assert after.status is TicketStatus.resolved and after.closed_at is not None
    final = await fetch(session_factory, ticket.id)
    await press(deps, fake, ADMIN, "take", ticket.id, final.version)
    assert fake.last_alert_text() == "Обращение изменилось" or fake.last_alert_text() == "Уже взято"


async def test_post_sync_failure_does_not_break_action(session_factory) -> None:
    await mk_user(session_factory, SUPPORT, Role.support)
    await mk_user(session_factory, DEV, Role.developer)
    ticket = await mk_ticket(session_factory)
    deps, fake = make_deps(session_factory)

    fake.script(RuntimeError("telegram down"))  # the sync edit/send raises
    await press(deps, fake, DEV, "take", ticket.id, 1)  # must not propagate

    after = await fetch(session_factory, ticket.id)
    assert after.status is TicketStatus.in_progress
    assert fake.last_alert_text() == "Готово"


async def test_parse_ticket_callback_rejects_garbage() -> None:
    assert parse_ticket_callback("t:take:12:3") == (Action.take, 12, 3)
    for bad in ("t:take:12", "d:take:12:3", "t:nope:12:3", "t:take:xx:3"):
        try:
            parse_ticket_callback(bad)
            raise AssertionError(f"should have raised: {bad}")
        except ValueError:
            pass


async def test_route_callback_bad_data_toasts(session_factory) -> None:
    await mk_user(session_factory, SUPPORT, Role.support)
    deps, fake = make_deps(session_factory)
    await route_callback(deps, user_id=SUPPORT, callback_id="cbx", chat_id=SUPPORT, data="t:junk")
    assert fake.last_alert_text() == "Кнопка не распознана"
    await route_callback(
        deps, user_id=SUPPORT, callback_id="cby", chat_id=SUPPORT, data="d:notauuid:take:1"
    )
    assert "не распознана" in (fake.last_alert_text() or "")
    async with session_factory() as s:
        assert len(list(await s.scalars(select(Ticket)))) == 0
