from __future__ import annotations

import asyncio

import pytest

from support_bot.db.enums import UserRole
from support_bot.db.locks import advisory_lock
from support_bot.db.models.ticket import Ticket
from support_bot.services import tickets as tickets_service
from support_bot.services.post_renderer import (
    render_ticket_post,
    truncate_html_utf16,
    utf16_len,
)
from support_bot.workers.post_sync import post_sync_tick


async def _load_with_relations(session_factory, ticket_id: int) -> Ticket:
    async with session_factory() as session:
        return await tickets_service.get_ticket_with_relations(session, ticket_id)


async def test_render_fits_telegram_limit(session_factory, make_user, make_ticket) -> None:
    author = await make_user(
        telegram_id=200,
        role=UserRole.SUPPORT,
        first_name="Очень " * 60 + "длинное имя",
        username="very_long_username_" * 3,
    )
    description = ("😀" * 1500) + "x" * 1000
    ticket = await make_ticket(author_id=author.id, description=description)
    loaded = await _load_with_relations(session_factory, ticket.id)
    text, keyboard = render_ticket_post(loaded)
    assert utf16_len(text) <= 4096
    assert text.endswith("…")
    assert keyboard is not None


async def test_render_escapes_description_and_keeps_entities_intact(
    session_factory, make_user, make_ticket
) -> None:
    author = await make_user(telegram_id=205, role=UserRole.SUPPORT)
    description = "<b>" + ("&" * 1200) + '"' + "x" * 1000
    ticket = await make_ticket(author_id=author.id, description=description, request_id="HTML-1")
    loaded = await _load_with_relations(session_factory, ticket.id)
    text, _ = render_ticket_post(loaded)

    assert "&lt;b&gt;" in text
    assert "&amp;" in text
    assert utf16_len(text) <= 4096
    body = text.split("Описание:\n", 1)[1].removesuffix("…")
    assert "<b>" not in body
    ampersand = body.rfind("&")
    assert ampersand == -1 or ";" in body[ampersand:]


def test_truncate_html_utf16_does_not_split_entity() -> None:
    text = "a" * 10 + "&amp;" * 10
    truncated = truncate_html_utf16(text, 14)
    assert utf16_len(truncated) <= 14
    body = truncated.removesuffix("…")
    ampersand = body.rfind("&")
    assert ampersand == -1 or ";" in body[ampersand:]


async def test_sync_publishes_and_saves_rendered_version(
    session_factory, publisher, post_sync, make_user, make_ticket
) -> None:
    author = await make_user(telegram_id=201, role=UserRole.SUPPORT)
    ticket = await make_ticket(author_id=author.id)

    assert await post_sync.sync(ticket.id) is True
    assert len(publisher.sent) == 1
    message_id, text, keyboard = publisher.sent[0]
    assert f"#{ticket.id}" in text
    assert keyboard is not None

    async with session_factory() as session:
        current = await session.get(Ticket, ticket.id)
    assert current is not None
    assert current.message_id == message_id
    assert current.post_synced_version == 1
    assert current.version == 1


async def test_synced_ticket_is_not_selected_again(
    session_factory, publisher, post_sync, make_user, make_ticket
) -> None:
    author = await make_user(telegram_id=202, role=UserRole.SUPPORT)
    ticket = await make_ticket(author_id=author.id)
    await post_sync.sync(ticket.id)
    publisher.sent.clear()

    synced = await post_sync_tick(session_factory, post_sync, batch_size=10)
    assert synced == 0
    assert publisher.sent == []


async def test_business_change_during_send_causes_resync(
    session_factory, publisher, post_sync, make_user, make_ticket
) -> None:
    author = await make_user(telegram_id=203, role=UserRole.SUPPORT)
    developer = await make_user(telegram_id=204, role=UserRole.DEVELOPER)
    ticket = await make_ticket(author_id=author.id)

    async def take_during_send(call_index: int) -> None:
        if call_index != 1:
            return
        async with session_factory() as session:
            await tickets_service.take_ticket(
                session,
                ticket_id=ticket.id,
                expected_version=1,
                actor_id=developer.id,
            )

    publisher.send_hook = take_during_send
    await post_sync.sync(ticket.id)

    async with session_factory() as session:
        current = await session.get(Ticket, ticket.id)
    assert current is not None
    assert current.version == 2
    assert current.post_synced_version == 1

    # The background worker sees the lag and renders the fresh state.
    publisher.send_hook = None
    synced = await post_sync_tick(session_factory, post_sync, batch_size=10)
    assert synced == 1
    assert "В работе" in publisher.edited[-1][2]

    async with session_factory() as session:
        current = await session.get(Ticket, ticket.id)
    assert current is not None
    assert current.post_synced_version == 2


async def test_concurrent_syncs_serialize_and_latest_wins(
    session_factory, publisher, post_sync, make_user, make_ticket
) -> None:
    author = await make_user(telegram_id=205, role=UserRole.SUPPORT)
    developer = await make_user(telegram_id=206, role=UserRole.DEVELOPER)
    ticket = await make_ticket(author_id=author.id)

    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_send(call_index: int) -> None:
        if call_index == 1:
            started.set()
            await release.wait()

    publisher.send_hook = blocking_send
    first = asyncio.create_task(post_sync.sync(ticket.id))
    await asyncio.wait_for(started.wait(), timeout=2)

    async with session_factory() as session:
        await tickets_service.take_ticket(
            session, ticket_id=ticket.id, expected_version=1, actor_id=developer.id
        )

    second = asyncio.create_task(post_sync.sync(ticket.id))
    await asyncio.sleep(0.1)
    release.set()
    assert await asyncio.wait_for(first, timeout=3) is True
    assert await asyncio.wait_for(second, timeout=3) is True

    async with session_factory() as session:
        current = await session.get(Ticket, ticket.id)
    assert current is not None
    assert current.post_synced_version == 2
    assert "В работе" in publisher.edited[-1][2]


async def test_lock_is_released_after_telegram_error(
    session_factory, publisher, post_sync, settings, make_user, make_ticket
) -> None:
    author = await make_user(telegram_id=207, role=UserRole.SUPPORT)
    ticket = await make_ticket(author_id=author.id)
    publisher.fail_send = True
    with pytest.raises(RuntimeError):
        await post_sync.sync(ticket.id)

    publisher.fail_send = False
    async with advisory_lock(settings.database_dsn, ticket.id, wait_timeout=1.0, try_only=True):
        pass
    assert await post_sync.sync(ticket.id) is True


async def test_lock_is_released_on_cancellation(
    session_factory, publisher, post_sync, settings, make_user, make_ticket
) -> None:
    author = await make_user(telegram_id=208, role=UserRole.SUPPORT)
    ticket = await make_ticket(author_id=author.id)

    started = asyncio.Event()
    never = asyncio.Event()

    async def blocking_send(call_index: int) -> None:
        started.set()
        await never.wait()

    publisher.send_hook = blocking_send
    task = asyncio.create_task(post_sync.sync(ticket.id))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # If the lock leaked, this would time out.
    async with advisory_lock(settings.database_dsn, ticket.id, wait_timeout=2.0, try_only=True):
        pass


async def test_force_new_replaces_message_id(
    session_factory, publisher, post_sync, make_user, make_ticket
) -> None:
    author = await make_user(telegram_id=209, role=UserRole.SUPPORT)
    ticket = await make_ticket(author_id=author.id)
    await post_sync.sync(ticket.id)
    first_id = publisher.sent[0][0]

    assert await post_sync.sync(ticket.id, force_new=True) is True
    assert len(publisher.sent) == 2
    assert publisher.sent[1][0] != first_id

    async with session_factory() as session:
        current = await session.get(Ticket, ticket.id)
    assert current is not None
    assert current.message_id == publisher.sent[1][0]
    assert current.version == 1


async def test_deleted_post_is_replaced(
    session_factory, publisher, post_sync, make_user, make_ticket
) -> None:
    author = await make_user(telegram_id=210, role=UserRole.SUPPORT)
    ticket = await make_ticket(author_id=author.id)
    await post_sync.sync(ticket.id)

    publisher.edit_missing = True
    assert await post_sync.sync(ticket.id, force_new=False) is True
    # No-op: post_synced_version already equals version, but an explicit sync still edits.
    assert len(publisher.sent) == 2
    async with session_factory() as session:
        current = await session.get(Ticket, ticket.id)
    assert current is not None
    assert current.message_id == publisher.sent[1][0]


async def test_background_publishes_missing_post(
    session_factory, publisher, post_sync, make_user, make_ticket
) -> None:
    author = await make_user(telegram_id=211, role=UserRole.SUPPORT)
    ticket = await make_ticket(author_id=author.id)
    synced = await post_sync_tick(session_factory, post_sync, batch_size=10)
    assert synced == 1
    async with session_factory() as session:
        current = await session.get(Ticket, ticket.id)
    assert current is not None
    assert current.message_id == publisher.sent[0][0]
