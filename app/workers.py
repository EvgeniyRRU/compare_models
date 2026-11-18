# app/workers.py
"""Фоновые задачи: работают в каждой реплике; истина — в БД."""

import asyncio
from typing import Any

import sentry_sdk
from sqlalchemy import select

from app.config import Settings
from app.db import DB
from app.log import get_logger
from app.metrics import errors_total
from app.models import Ticket
from app.services.post_sync import sync_ticket_post

log = get_logger("workers")


def _report(component: str, exc: BaseException) -> None:
    log.error("worker error", component=component, exc_info=exc)
    errors_total.labels(component=component).inc()
    sentry_sdk.capture_exception(exc)


async def post_sync_iteration(settings: Settings, db: DB, bot: Any) -> None:
    """Одна итерация: выбрать несинхронизированные и синхронизировать
    (skip при занятой блокировке)."""
    async with db.session_factory() as session:
        ids = (
            (
                await session.execute(
                    select(Ticket.id)
                    .where(
                        (Ticket.message_id.is_(None))
                        | (Ticket.post_synced_version < Ticket.version)
                    )
                    .order_by(Ticket.id)
                    .limit(50)
                )
            )
            .scalars()
            .all()
        )
    for ticket_id in ids:
        try:
            await sync_ticket_post(settings, db, bot, ticket_id, lock_timeout=0.0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _report("post_sync", exc)


async def run_post_sync_loop(settings: Settings, db: DB, bot: Any) -> None:
    while True:
        await asyncio.sleep(settings.post_sync_interval_seconds)
        try:
            await post_sync_iteration(settings, db, bot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _report("post_sync_loop", exc)


async def run_draft_ttl_loop(settings: Settings, db: DB) -> None:
    from app.services.drafts import delete_expired_drafts

    while True:
        await asyncio.sleep(3600)
        try:
            removed = await delete_expired_drafts(db.session_factory, settings.draft_ttl_hours)
            if removed:
                log.info("expired drafts removed", count=removed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _report("draft_ttl", exc)


async def run_processed_updates_cleanup(db: DB) -> None:
    from app.services.updates import purge_processed_updates

    while True:
        await asyncio.sleep(3600)
        try:
            await purge_processed_updates(db)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _report("dedup_cleanup", exc)


async def run_reminder_loop(settings: Settings, db: DB, bot: Any) -> None:
    from app.services.reminders import claim_due_reminders, send_reminders

    while True:
        await asyncio.sleep(settings.reminder_poll_interval_seconds)
        try:
            claims = await claim_due_reminders(db.session_factory, settings)
            if claims:
                await send_reminders(settings, bot, claims)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _report("reminders", exc)
