"""SQLAlchemy ORM models.

Importing this module registers every model on :data:`app.db.base.Base.metadata`,
which Alembic autogenerate and ``Base.metadata.create_all`` both rely on.
"""

from app.db.base import Base
from app.db.models.processed_update import ProcessedUpdate
from app.db.models.ticket import Ticket
from app.db.models.ticket_draft import TicketDraft
from app.db.models.ticket_event import TicketEvent
from app.db.models.user import User

__all__ = [
    "Base",
    "User",
    "Ticket",
    "TicketEvent",
    "TicketDraft",
    "ProcessedUpdate",
]
