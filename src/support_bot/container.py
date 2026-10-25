"""Сборка зависимостей приложения."""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from support_bot.config import Settings
from support_bot.db.engine import SessionFactory, create_session_factory
from support_bot.services.cleanup import CleanupService
from support_bot.services.drafts import DraftService
from support_bot.services.ports import PostPublisher
from support_bot.services.post_sync import PostSyncService
from support_bot.services.reminders import ReminderService
from support_bot.services.tickets import TicketService
from support_bot.services.updates_dedup import UpdateDeduplicator
from support_bot.services.users import UserService


@dataclass
class Container:
    settings: Settings
    engine: AsyncEngine
    session_factory: SessionFactory
    users: UserService
    tickets: TicketService
    drafts: DraftService
    post_sync: PostSyncService
    reminders: ReminderService
    dedup: UpdateDeduplicator
    cleanup: CleanupService


def build_container(settings: Settings, engine: AsyncEngine, publisher: PostPublisher) -> Container:
    sf = create_session_factory(engine)
    tickets = TicketService(sf, settings.reminder_intervals)
    drafts = DraftService(sf, tickets, settings.draft_ttl)
    dedup = UpdateDeduplicator(sf)
    return Container(
        settings=settings,
        engine=engine,
        session_factory=sf,
        users=UserService(sf, settings.bootstrap_admin_ids),
        tickets=tickets,
        drafts=drafts,
        post_sync=PostSyncService(
            engine,
            sf,
            publisher,
            lock_timeout=settings.post_sync_lock_timeout_seconds,
            batch_size=settings.post_sync_batch_size,
        ),
        reminders=ReminderService(sf, tickets, publisher, batch_size=settings.reminder_batch_size),
        dedup=dedup,
        cleanup=CleanupService(drafts, dedup, settings.processed_updates_ttl),
    )
