import asyncio

import pytest

from support_bot.observability.metrics import Metrics, OpenTicketsCollector, render_metrics
from support_bot.services.tickets import TicketService
from support_bot.services.users import UserService

pytestmark = pytest.mark.integration


async def test_open_ticket_gauge_queries_db_on_every_scrape(db, settings):
    metrics = Metrics()
    metrics.registry.register(OpenTicketsCollector(db.sessions, asyncio.get_running_loop()))
    first, _ = await render_metrics(metrics.registry)
    assert b'support_open_tickets{status="new"} 0.0' in first
    settings.bootstrap_admin_ids = [42]
    admin = await UserService(db.sessions, settings).touch(42, "Admin", bootstrap=True)
    tickets = TicketService(db.sessions, settings)
    ticket = await tickets.create(admin.id, "Description", "low", "R")
    second, _ = await render_metrics(metrics.registry)
    assert b'support_open_tickets{status="new"} 1.0' in second
    await tickets.change(admin.id, ticket.id, 1, "new", "take")
    third, _ = await render_metrics(metrics.registry)
    assert b'support_open_tickets{status="new"} 0.0' in third
    assert b'support_open_tickets{status="in_progress"} 1.0' in third
