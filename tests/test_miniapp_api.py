import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import httpx
import pytest
from sqlalchemy import delete

from app.core.config import get_settings
from app.db.models import Priority, Role, Ticket, TicketEvent, User
from app.db.session import SessionLocal
from app.main import app
from app.services.tickets import create_ticket


def auth_header(telegram_id: int) -> str:
    fields = {
        "auth_date": str(int(time.time())),
        "user": json.dumps(
            {"id": telegram_id, "first_name": "API", "last_name": "Tester"}, separators=(",", ":")
        ),
    }
    data_check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", get_settings().bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    return "tma " + urlencode(fields)


@pytest.mark.asyncio
async def test_miniapp_lists_all_tickets_and_filters_mine():
    telegram_id = int(time.time() * 1_000_000)
    async with SessionLocal.begin() as session:
        user = User(telegram_id=telegram_id, role=Role.SUPPORT, full_name="API Tester")
        other = User(telegram_id=telegram_id + 1, role=Role.SUPPORT, full_name="Other")
        session.add_all([user, other])
        await session.flush()
        own = await create_ticket(
            session,
            author=user,
            description="Own ticket",
            priority=Priority.HIGH,
            request_id=f"OWN-{telegram_id}",
            tenant_id=None,
        )
        shared = await create_ticket(
            session,
            author=other,
            description="Other ticket",
            priority=Priority.LOW,
            request_id=f"OTHER-{telegram_id}",
            tenant_id=None,
        )
        user_id, other_id, own_id, shared_id = user.id, other.id, own.id, shared.id
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = {"Authorization": auth_header(telegram_id)}
            all_tickets = await client.get("/api/tickets", headers=headers)
            mine = await client.get("/api/tickets?mine=true", headers=headers)
            assert all_tickets.status_code == mine.status_code == 200
            assert {ticket["id"] for ticket in all_tickets.json()} >= {own_id, shared_id}
            assert {ticket["id"] for ticket in mine.json()} == {own_id}
            detail = await client.get(f"/api/tickets/{own_id}", headers=headers)
            assert detail.json()["description"] == "Own ticket"
            assert detail.json()["author_name"] == "API Tester"
            assert detail.json()["updated_at"]
            assert detail.json()["post_synced_version"] == 0
    finally:
        async with SessionLocal.begin() as session:
            await session.execute(
                delete(TicketEvent).where(TicketEvent.ticket_id.in_([own_id, shared_id]))
            )
            await session.execute(delete(Ticket).where(Ticket.id.in_([own_id, shared_id])))
            await session.execute(delete(User).where(User.id.in_([user_id, other_id])))
