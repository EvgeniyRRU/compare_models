from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


async def test_update_deduplicates_before_processing(settings):
    from support_bot.runtime import UpdateProcessor

    app = SimpleNamespace(process_update=AsyncMock())
    updates = SimpleNamespace(claim=AsyncMock(side_effect=[True, False]))
    processor = UpdateProcessor(app, updates, settings)
    update = SimpleNamespace(update_id=1, effective_user=None)
    assert await processor.process(update) is True
    assert await processor.process(update) is False
    app.process_update.assert_awaited_once_with(update)
    assert updates.claim.await_count == 2


async def test_update_failure_does_not_release_claim(settings):
    from support_bot.runtime import UpdateProcessor

    app = SimpleNamespace(process_update=AsyncMock(side_effect=ValueError("broken")))
    updates = SimpleNamespace(claim=AsyncMock(return_value=True))
    processor = UpdateProcessor(app, updates, settings)
    assert await processor.process(SimpleNamespace(update_id=3, effective_user=None)) is True
    updates.claim.assert_awaited_once_with(3)


async def test_update_cancellation_propagates(settings):
    import asyncio

    from support_bot.runtime import UpdateProcessor

    app = SimpleNamespace(process_update=AsyncMock(side_effect=asyncio.CancelledError()))
    processor = UpdateProcessor(app, SimpleNamespace(claim=AsyncMock(return_value=True)), settings)
    with pytest.raises(asyncio.CancelledError):
        await processor.process(SimpleNamespace(update_id=3, effective_user=None))


async def test_ticket_context_does_not_leak_between_updates(settings):
    from structlog.contextvars import bind_contextvars, get_contextvars

    from support_bot.runtime import UpdateProcessor

    seen = []

    async def handle(update):
        seen.append(get_contextvars().get("ticket_id"))
        bind_contextvars(ticket_id=42)

    processor = UpdateProcessor(
        SimpleNamespace(process_update=handle),
        SimpleNamespace(claim=AsyncMock(return_value=True)),
        settings,
    )
    await processor.process(SimpleNamespace(update_id=1, effective_user=None))
    await processor.process(SimpleNamespace(update_id=2, effective_user=None))
    assert seen == [None, None]
