from __future__ import annotations

import structlog

from support_bot.db.session import SessionFactory
from support_bot.services.cleanup import run_cleanup


async def cleanup_tick(
    session_factory: SessionFactory,
    *,
    draft_ttl_hours: int,
    updates_retention_hours: int,
) -> None:
    drafts, updates = await run_cleanup(
        session_factory,
        draft_ttl_hours=draft_ttl_hours,
        updates_retention_hours=updates_retention_hours,
    )
    if drafts or updates:
        structlog.get_logger(__name__).info(
            "cleanup finished", drafts=drafts, processed_updates=updates
        )
