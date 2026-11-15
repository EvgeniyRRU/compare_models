"""Tests for the Mini App ticket endpoints (real PostgreSQL)."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.enums import Priority, Role, TicketStatus
from app.db.models import Ticket, User
from app.main import create_app

from .api_helpers import auth_headers


@pytest.fixture
def api_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _redirect_api_db(
    monkeypatch: pytest.MonkeyPatch,
    engine: AsyncEngine,
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # ``get_session`` (used by the request-session dependency) reads
    # ``app.db.session.async_session_factory`` at call time, while the post-sync
    # helpers read the module-level ``engine``/``async_session_factory`` on the
    # tickets module. Point all three at the isolated test engine.
    monkeypatch.setattr("app.db.session.async_session_factory", api_session_factory)
    monkeypatch.setattr("app.api.routes.tickets.engine", engine)
    monkeypatch.setattr("app.api.routes.tickets.async_session_factory", api_session_factory)


@pytest.fixture
async def app(engine: AsyncEngine) -> AsyncIterator[FastAPI]:
    yield create_app(engine=engine)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class FakePostClient:
    """Records send/edit calls and hands out incrementing message ids."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.sent_ids: list[int] = []
        self.edited: list[tuple[int, str]] = []
        self._next_id = 1000

    async def send_post(self, text: str, reply_markup: Any | None = None) -> int:
        self.sent.append(text)
        self._next_id += 1
        self.sent_ids.append(self._next_id)
        return self._next_id

    async def edit_post(self, message_id: int, text: str, reply_markup: Any | None = None) -> int:
        self.edited.append((message_id, text))
        return message_id


async def mk_user(session: AsyncSession, telegram_id: int, role: Role | None = None) -> User:
    user = User(telegram_id=telegram_id, role=role)
    session.add(user)
    await session.flush()
    return user


async def mk_ticket(
    session: AsyncSession,
    author: User,
    *,
    assignee: User | None = None,
    status: TicketStatus = TicketStatus.new,
    priority: Priority = Priority.medium,
    request_id: str = "REQ-1",
) -> Ticket:
    active = status in (TicketStatus.in_progress, TicketStatus.waiting_info)
    final = status in (TicketStatus.resolved, TicketStatus.rejected, TicketStatus.duplicate)
    ticket = Ticket(
        author_id=author.id,
        assignee_id=assignee.id if assignee is not None else None,
        description="a test ticket",
        priority=priority,
        request_id=request_id,
        status=status,
        version=1,
        taken_at=datetime.now(UTC) if active else None,
        closed_at=datetime.now(UTC) if final else None,
    )
    session.add(ticket)
    await session.flush()
    return ticket


async def fresh_ticket(session: AsyncSession, ticket_id: int) -> Ticket:
    stmt = select(Ticket).where(Ticket.id == ticket_id).execution_options(populate_existing=True)
    return (await session.execute(stmt)).scalars().one()


async def test_list_authored_scope(session: AsyncSession, client: httpx.AsyncClient) -> None:
    support = await mk_user(session, 2001, Role.support)
    developer = await mk_user(session, 2002, Role.developer)
    await session.commit()
    mine = await mk_ticket(session, support, priority=Priority.high)
    await mk_ticket(session, developer, priority=Priority.low)
    await session.commit()

    response = await client.get("/api/tickets", headers=auth_headers(2001))

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert [item["id"] for item in body["items"]] == [mine.id]


async def test_list_assigned_scope(session: AsyncSession, client: httpx.AsyncClient) -> None:
    support = await mk_user(session, 2001, Role.support)
    developer = await mk_user(session, 2002, Role.developer)
    await session.commit()
    assigned = await mk_ticket(
        session, support, assignee=developer, status=TicketStatus.in_progress
    )
    await mk_ticket(session, support, status=TicketStatus.new)
    await session.commit()

    response = await client.get("/api/tickets?scope=assigned", headers=auth_headers(2002))

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert [item["id"] for item in body["items"]] == [assigned.id]


async def test_list_all_scope_requires_admin(
    session: AsyncSession, client: httpx.AsyncClient
) -> None:
    support = await mk_user(session, 2001, Role.support)
    await mk_user(session, 2003, Role.admin)
    await session.commit()
    await mk_ticket(session, support)
    await session.commit()

    denied = await client.get("/api/tickets?scope=all", headers=auth_headers(2001))
    assert denied.status_code == 403

    allowed = await client.get("/api/tickets?scope=all", headers=auth_headers(2003))
    assert allowed.status_code == 200
    assert allowed.json()["total"] == 1


async def test_list_status_and_priority_filters(
    session: AsyncSession, client: httpx.AsyncClient
) -> None:
    support = await mk_user(session, 2001, Role.support)
    await session.commit()
    high = await mk_ticket(session, support, priority=Priority.high)
    low = await mk_ticket(session, support, priority=Priority.low)
    await session.commit()

    by_status = await client.get("/api/tickets?status=new", headers=auth_headers(2001))
    assert by_status.json()["total"] == 2

    by_priority = await client.get("/api/tickets?priority=high", headers=auth_headers(2001))
    assert by_priority.json()["total"] == 1
    assert by_priority.json()["items"][0]["id"] == high.id

    combined = await client.get("/api/tickets?status=new&priority=low", headers=auth_headers(2001))
    assert combined.json()["total"] == 1
    assert combined.json()["items"][0]["id"] == low.id


async def test_list_pagination(session: AsyncSession, client: httpx.AsyncClient) -> None:
    support = await mk_user(session, 2001, Role.support)
    await session.commit()
    for i in range(5):
        await mk_ticket(session, support, request_id=f"REQ-{i}")
    await session.commit()

    response = await client.get("/api/tickets?page=2&page_size=2", headers=auth_headers(2001))

    body = response.json()
    assert body["total"] == 5
    assert body["page"] == 2
    assert body["page_size"] == 2
    assert len(body["items"]) == 2


async def test_list_orders_desc(session: AsyncSession, client: httpx.AsyncClient) -> None:
    support = await mk_user(session, 2001, Role.support)
    await session.commit()
    ids = []
    for i in range(3):
        ticket = await mk_ticket(session, support, request_id=f"REQ-{i}")
        ids.append(ticket.id)
    await session.commit()

    response = await client.get("/api/tickets", headers=auth_headers(2001))

    returned = [item["id"] for item in response.json()["items"]]
    assert returned == sorted(ids, reverse=True)


async def test_get_ticket_detail_and_404(session: AsyncSession, client: httpx.AsyncClient) -> None:
    support = await mk_user(session, 2001, Role.support)
    await session.commit()
    ticket = await mk_ticket(session, support, priority=Priority.critical)
    await session.commit()

    response = await client.get(f"/api/tickets/{ticket.id}", headers=auth_headers(2001))
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == ticket.id
    assert body["priority"] == "critical"
    assert body["status"] == "new"
    assert body["description"] == "a test ticket"

    missing = await client.get("/api/tickets/999999", headers=auth_headers(2001))
    assert missing.status_code == 404


async def test_reopen_happy_path(session: AsyncSession, client: httpx.AsyncClient) -> None:
    support = await mk_user(session, 2001, Role.support)
    await mk_user(session, 2003, Role.admin)
    await session.commit()
    ticket = await mk_ticket(session, support, status=TicketStatus.resolved)
    await session.commit()

    response = await client.post(
        f"/api/tickets/{ticket.id}/reopen",
        json={"version": 1},
        headers=auth_headers(2003),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "new"
    assert body["version"] == 2
    assert body["assignee_id"] is None
    assert body["closed_at"] is None


async def test_reopen_stale_version_409(session: AsyncSession, client: httpx.AsyncClient) -> None:
    support = await mk_user(session, 2001, Role.support)
    await mk_user(session, 2003, Role.admin)
    await session.commit()
    ticket = await mk_ticket(session, support, status=TicketStatus.resolved)
    await session.commit()

    response = await client.post(
        f"/api/tickets/{ticket.id}/reopen",
        json={"version": 999},
        headers=auth_headers(2003),
    )

    assert response.status_code == 409


async def test_reopen_non_admin_403(session: AsyncSession, client: httpx.AsyncClient) -> None:
    support = await mk_user(session, 2001, Role.support)
    await mk_user(session, 2002, Role.developer)
    await session.commit()
    ticket = await mk_ticket(session, support, status=TicketStatus.resolved)
    await session.commit()

    response = await client.post(
        f"/api/tickets/{ticket.id}/reopen",
        json={"version": 1},
        headers=auth_headers(2002),
    )

    assert response.status_code == 403


async def test_reassign_happy_path(session: AsyncSession, client: httpx.AsyncClient) -> None:
    support = await mk_user(session, 2001, Role.support)
    dev1 = await mk_user(session, 2002, Role.developer)
    dev2 = await mk_user(session, 2003, Role.developer)
    await mk_user(session, 2004, Role.admin)
    await session.commit()
    ticket = await mk_ticket(session, support, assignee=dev1, status=TicketStatus.in_progress)
    await session.commit()

    response = await client.post(
        f"/api/tickets/{ticket.id}/reassign",
        json={"version": 1, "assignee_id": dev2.id},
        headers=auth_headers(2004),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["assignee_id"] == dev2.id
    assert body["version"] == 2
    assert body["status"] == "in_progress"


async def test_reassign_stale_version_409(session: AsyncSession, client: httpx.AsyncClient) -> None:
    support = await mk_user(session, 2001, Role.support)
    dev1 = await mk_user(session, 2002, Role.developer)
    dev2 = await mk_user(session, 2003, Role.developer)
    await mk_user(session, 2004, Role.admin)
    await session.commit()
    ticket = await mk_ticket(session, support, assignee=dev1, status=TicketStatus.in_progress)
    await session.commit()

    response = await client.post(
        f"/api/tickets/{ticket.id}/reassign",
        json={"version": 999, "assignee_id": dev2.id},
        headers=auth_headers(2004),
    )

    assert response.status_code == 409


async def test_reassign_non_admin_403(session: AsyncSession, client: httpx.AsyncClient) -> None:
    support = await mk_user(session, 2001, Role.support)
    dev1 = await mk_user(session, 2002, Role.developer)
    dev2 = await mk_user(session, 2003, Role.developer)
    await session.commit()
    ticket = await mk_ticket(session, support, assignee=dev1, status=TicketStatus.in_progress)
    await session.commit()

    response = await client.post(
        f"/api/tickets/{ticket.id}/reassign",
        json={"version": 1, "assignee_id": dev2.id},
        headers=auth_headers(2002),
    )

    assert response.status_code == 403


async def test_restore_post_sends_new_message(
    session: AsyncSession, app: FastAPI, client: httpx.AsyncClient
) -> None:
    support = await mk_user(session, 2001, Role.support)
    await mk_user(session, 2003, Role.admin)
    await session.commit()
    ticket = await mk_ticket(session, support)
    await session.commit()

    # Give the ticket an existing post that restore-post must abandon.
    await session.execute(
        update(Ticket).where(Ticket.id == ticket.id).values(message_id=111, post_synced_version=1)
    )
    await session.commit()

    fake = FakePostClient()
    app.state.post_client = fake

    response = await client.post(
        f"/api/tickets/{ticket.id}/restore-post",
        headers=auth_headers(2003),
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert len(fake.sent) == 1

    fresh = await fresh_ticket(session, ticket.id)
    assert fresh.message_id == fake.sent_ids[0]
    assert fresh.message_id != 111
