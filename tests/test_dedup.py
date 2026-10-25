from typing import Any

from sqlalchemy import select, text

from support_bot.db.engine import SessionFactory
from support_bot.db.models import ProcessedUpdate
from support_bot.services.updates_dedup import UpdateDeduplicator
from support_bot.telegram.pipeline import UpdatePipeline


async def test_register_once(sf: SessionFactory) -> None:
    dedup = UpdateDeduplicator(sf)
    assert await dedup.register(1)
    assert not await dedup.register(1)
    assert await dedup.register(2)


async def test_pipeline_skips_duplicates_and_error_keeps_record(sf: SessionFactory) -> None:
    dedup = UpdateDeduplicator(sf)
    calls: list[int] = []

    async def failing(update: Any) -> None:
        calls.append(update.update_id)
        # Бизнес-обработка в своей транзакции падает…
        async with sf.begin() as s:
            await s.execute(text("SELECT 1"))
            raise RuntimeError("handler failed")

    pipeline = UpdatePipeline(dedup, failing, timeout=5)

    class U:
        update_id = 77

    assert await pipeline.handle(U()) == "failed"
    # …но запись о дедупликации уже закоммичена и не откатилась.
    async with sf() as s:
        assert await s.scalar(select(ProcessedUpdate.update_id)) == 77
    assert await pipeline.handle(U()) == "duplicate"
    assert calls == [77]


async def test_pipeline_timeout(sf: SessionFactory) -> None:
    import asyncio

    async def slow(update: Any) -> None:
        await asyncio.sleep(10)

    pipeline = UpdatePipeline(UpdateDeduplicator(sf), slow, timeout=0.1)

    class U:
        update_id = 5

    assert await pipeline.handle(U()) == "timeout"


async def test_cleanup_old_records(sf: SessionFactory) -> None:
    from datetime import timedelta

    dedup = UpdateDeduplicator(sf)
    await dedup.register(1)
    await dedup.register(2)
    async with sf.begin() as s:
        await s.execute(
            text(
                "UPDATE processed_updates SET processed_at = now() - interval '49 hours' "
                "WHERE update_id = 1"
            )
        )
    assert await dedup.delete_older_than(timedelta(hours=48)) == 1
    assert not await dedup.register(2)
    assert await dedup.register(1)
