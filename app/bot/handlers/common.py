"""Shared handler plumbing: dependency bag and small helpers.

The cores (create_form, ticket_actions) are pure async functions over Gateway +
session factory so they are testable with FakeGateway without PTB machinery;
handlers/router.py glues parsed updates to cores, application.py adapts PTB to it.
"""

from typing import NamedTuple

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.telegram_gateway import Gateway


class Deps(NamedTuple):
    gateway: Gateway
    session_factory: async_sessionmaker[AsyncSession]
    settings: Settings


async def toast(
    gateway: Gateway, callback_id: str, text: str | None, *, alert: bool = False
) -> None:
    await gateway.answer_callback(callback_id, text, show_alert=alert)


def parse_command(text: str) -> str:
    """'/new@botname arg' -> 'new'."""
    head = text.split(maxsplit=1)[0]
    return head.lstrip("/").split("@", 1)[0].lower()
