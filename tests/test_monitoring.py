import asyncio

import httpx
from fastapi import FastAPI
from prometheus_client import REGISTRY

from support_bot.db.engine import SessionFactory
from support_bot.domain.enums import Role
from support_bot.domain.state_machine import Action
from support_bot.monitoring.collector import OpenTicketsCollector
from support_bot.monitoring.middleware import ObservabilityMiddleware
from support_bot.services.tickets import TicketService
from tests.conftest import MakeTicket, MakeUser


async def test_route_label_uses_template() -> None:
    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware)

    @app.get("/items/{item_id}")
    async def item(item_id: int) -> dict[str, int]:
        return {"id": item_id}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        for i in (1, 2, 3):
            assert (await c.get(f"/items/{i}")).status_code == 200
        await c.get("/nope")

    def value(route: str, status: str) -> float | None:
        return REGISTRY.get_sample_value(
            "support_bot_http_requests_total", {"method": "GET", "route": route, "status": status}
        )

    assert (value("/items/{item_id}", "200") or 0) >= 3
    assert value("/items/1", "200") is None
    assert (value("unmatched", "404") or 0) >= 1


async def test_open_tickets_collector(
    sf: SessionFactory, tickets: TicketService, make_user: MakeUser, make_ticket: MakeTicket
) -> None:
    support, dev = await make_user(Role.SUPPORT), await make_user(Role.DEVELOPER)
    a, b, c = await make_ticket(support), await make_ticket(support), await make_ticket(support)
    await tickets.apply_action(dev.id, b, Action.TAKE, 1)
    await tickets.apply_action(dev.id, c, Action.TAKE, 1)
    await tickets.apply_action(dev.id, c, Action.RESOLVE, 2)

    collector = OpenTicketsCollector(sf, asyncio.get_running_loop())
    # collect() вызывается из потока HTTP-сервера метрик
    (family,) = await asyncio.to_thread(lambda: list(collector.collect()))
    samples = {s.labels["status"]: s.value for s in family.samples}
    assert samples == {"new": 1, "in_progress": 1, "waiting_info": 0}
    assert a
