from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.services.drafts import DraftService
from app.services.post_sync import PostSyncer
from app.services.publisher import Publisher
from app.services.tickets import TicketService


@dataclass
class AppContext:
    settings: Settings
    engine: AsyncEngine
    sm: async_sessionmaker[AsyncSession]
    tickets: TicketService
    drafts: DraftService
    syncer: PostSyncer
    publisher: Publisher
