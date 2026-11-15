"""Tests for the draft service (real PostgreSQL via the conftest fixtures)."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.enums import DraftStep, Priority, Role, TicketEventType, TicketStatus
from app.db.models import Ticket, TicketDraft, TicketEvent, User
from app.services.draft_service import (
    DraftChanged,
    DraftNotFound,
    DraftValidationError,
    cleanup_expired_drafts,
    confirm,
    create_draft,
    delete_draft,
    get_active_draft,
    get_draft,
    save_description,
    save_request_id,
    save_tenant_id,
    set_priority,
    set_prompt_message_id,
)
from app.services.ticket_service import PermissionDenied


async def mk_user(session: AsyncSession, telegram_id: int, role: Role | None = None) -> User:
    user = User(telegram_id=telegram_id, role=role)
    session.add(user)
    await session.flush()
    return user


async def new_draft(session: AsyncSession, user: User) -> TicketDraft:
    draft = await create_draft(session, user_id=user.id)
    await session.commit()
    return draft


async def fresh_draft(session: AsyncSession, draft_id: uuid.UUID) -> TicketDraft:
    """Load a draft with fresh values, bypassing the identity map."""
    stmt = (
        select(TicketDraft)
        .where(TicketDraft.id == draft_id)
        .execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).scalars().one()


async def advance_to_tenant_id(
    session: AsyncSession,
    draft: TicketDraft,
    *,
    description: str = "desc",
    priority: Priority = Priority.medium,
    request_id: str = "REQ-1",
) -> TicketDraft:
    """Advance a fresh draft through description/priority/request_id (version 4)."""
    draft = await save_description(
        session,
        draft_id=draft.id,
        user_id=draft.user_id,
        expected_version=draft.version,
        description=description,
    )
    draft = await set_priority(
        session,
        draft_id=draft.id,
        user_id=draft.user_id,
        expected_version=draft.version,
        priority=priority,
    )
    draft = await save_request_id(
        session,
        draft_id=draft.id,
        user_id=draft.user_id,
        expected_version=draft.version,
        request_id=request_id,
    )
    return draft


async def fill_draft(
    session: AsyncSession,
    draft: TicketDraft,
    *,
    tenant_id: str | None = "tenant-1",
    description: str = "desc",
    priority: Priority = Priority.medium,
    request_id: str = "REQ-1",
) -> TicketDraft:
    """Advance a fresh draft all the way to the ``confirm`` step (version 5)."""
    draft = await advance_to_tenant_id(
        session,
        draft,
        description=description,
        priority=priority,
        request_id=request_id,
    )
    return await save_tenant_id(
        session,
        draft_id=draft.id,
        user_id=draft.user_id,
        expected_version=draft.version,
        tenant_id=tenant_id,
    )


async def event_count(session: AsyncSession, ticket_id: int) -> int:
    return cast(
        int,
        await session.scalar(
            select(func.count()).select_from(TicketEvent).where(TicketEvent.ticket_id == ticket_id)
        ),
    )


async def latest_event(session: AsyncSession, ticket_id: int) -> TicketEvent:
    stmt = (
        select(TicketEvent)
        .where(TicketEvent.ticket_id == ticket_id)
        .order_by(TicketEvent.id.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().one()


async def test_create_draft_fields(session: AsyncSession) -> None:
    user = await mk_user(session, 1001, Role.support)
    await session.commit()

    draft = await create_draft(session, user_id=user.id)
    await session.commit()

    assert isinstance(draft.id, uuid.UUID)
    assert draft.step is DraftStep.description
    assert draft.version == 1


async def test_get_active_draft_none_then_present(session: AsyncSession) -> None:
    user = await mk_user(session, 1001, Role.support)
    await session.commit()

    assert await get_active_draft(session, user.id) is None

    draft = await new_draft(session, user)
    found = await get_active_draft(session, user.id)
    assert found is not None
    assert found.id == draft.id


async def test_happy_path_full_flow(session: AsyncSession) -> None:
    user = await mk_user(session, 1001, Role.support)
    await session.commit()
    draft = await new_draft(session, user)

    # A prompt is recorded for the description step, then cleared on advance.
    assert (
        await set_prompt_message_id(
            session,
            draft_id=draft.id,
            expected_version=1,
            expected_step=DraftStep.description,
            message_id=111,
        )
        is True
    )

    draft = await save_description(
        session, draft_id=draft.id, user_id=user.id, expected_version=1, description="  hello  "
    )
    await session.commit()
    assert draft.version == 2
    assert draft.step is DraftStep.priority
    assert draft.description == "hello"
    assert draft.prompt_message_id is None

    draft = await set_priority(
        session, draft_id=draft.id, user_id=user.id, expected_version=2, priority=Priority.high
    )
    await session.commit()
    assert draft.version == 3
    assert draft.step is DraftStep.request_id
    assert draft.priority is Priority.high

    draft = await save_request_id(
        session, draft_id=draft.id, user_id=user.id, expected_version=3, request_id="REQ-1"
    )
    await session.commit()
    assert draft.version == 4
    assert draft.step is DraftStep.tenant_id
    assert draft.request_id == "REQ-1"

    draft = await save_tenant_id(
        session, draft_id=draft.id, user_id=user.id, expected_version=4, tenant_id="tenant-1"
    )
    await session.commit()
    assert draft.version == 5
    assert draft.step is DraftStep.confirm
    assert draft.tenant_id == "tenant-1"

    ticket = await confirm(session, draft_id=draft.id, expected_version=5, author=user)
    await session.commit()

    assert await get_draft(session, draft.id) is None
    assert ticket.status is TicketStatus.new
    assert ticket.version == 1
    assert ticket.next_reminder_at is not None
    assert ticket.description == "hello"
    assert ticket.priority is Priority.high
    assert ticket.request_id == "REQ-1"
    assert ticket.tenant_id == "tenant-1"

    assert await event_count(session, ticket.id) == 1
    event = await latest_event(session, ticket.id)
    assert event.event_type is TicketEventType.created


async def test_save_tenant_id_skip_none_and_empty(session: AsyncSession) -> None:
    user = await mk_user(session, 1001, Role.support)
    await session.commit()
    draft = await new_draft(session, user)
    draft = await advance_to_tenant_id(session, draft)

    draft = await save_tenant_id(
        session, draft_id=draft.id, user_id=user.id, expected_version=4, tenant_id=None
    )
    await session.commit()
    assert draft.step is DraftStep.confirm
    assert draft.tenant_id is None

    ticket = await confirm(session, draft_id=draft.id, expected_version=5, author=user)
    await session.commit()
    assert ticket.tenant_id is None

    # Empty/whitespace is also a skip.
    draft2 = await new_draft(session, user)
    draft2 = await advance_to_tenant_id(session, draft2)
    draft2 = await save_tenant_id(
        session, draft_id=draft2.id, user_id=user.id, expected_version=4, tenant_id="   "
    )
    await session.commit()
    assert draft2.tenant_id is None
    assert draft2.step is DraftStep.confirm


async def test_validation_errors_leave_draft_unchanged(session: AsyncSession) -> None:
    user = await mk_user(session, 1001, Role.support)
    await session.commit()
    draft = await new_draft(session, user)

    with pytest.raises(DraftValidationError):
        await save_description(
            session, draft_id=draft.id, user_id=user.id, expected_version=1, description="   "
        )
    with pytest.raises(DraftValidationError):
        await save_description(
            session, draft_id=draft.id, user_id=user.id, expected_version=1, description="x" * 3001
        )

    f = await fresh_draft(session, draft.id)
    assert f.version == 1
    assert f.step is DraftStep.description
    assert f.description is None

    draft = await save_description(
        session, draft_id=draft.id, user_id=user.id, expected_version=1, description="desc"
    )
    await session.commit()

    with pytest.raises(DraftValidationError):
        await save_request_id(
            session, draft_id=draft.id, user_id=user.id, expected_version=2, request_id="  "
        )
    with pytest.raises(DraftValidationError):
        await save_request_id(
            session, draft_id=draft.id, user_id=user.id, expected_version=2, request_id="r" * 65
        )

    f = await fresh_draft(session, draft.id)
    assert f.version == 2
    assert f.step is DraftStep.priority
    assert f.request_id is None

    draft = await set_priority(
        session, draft_id=draft.id, user_id=user.id, expected_version=2, priority=Priority.low
    )
    draft = await save_request_id(
        session, draft_id=draft.id, user_id=user.id, expected_version=3, request_id="REQ-1"
    )
    await session.commit()

    with pytest.raises(DraftValidationError):
        await save_tenant_id(
            session, draft_id=draft.id, user_id=user.id, expected_version=4, tenant_id="t" * 65
        )

    f = await fresh_draft(session, draft.id)
    assert f.version == 4
    assert f.step is DraftStep.tenant_id
    assert f.tenant_id is None


async def test_stale_version_raises_draft_changed(session: AsyncSession) -> None:
    user = await mk_user(session, 1001, Role.support)
    await session.commit()
    draft = await new_draft(session, user)
    draft = await advance_to_tenant_id(session, draft)  # version 4, step tenant_id
    await session.commit()

    with pytest.raises(DraftChanged):
        await save_request_id(
            session, draft_id=draft.id, user_id=user.id, expected_version=3, request_id="REQ-2"
        )

    f = await fresh_draft(session, draft.id)
    assert f.version == 4
    assert f.step is DraftStep.tenant_id
    assert f.request_id == "REQ-1"


async def test_wrong_step_raises_draft_changed(session: AsyncSession) -> None:
    user = await mk_user(session, 1001, Role.support)
    await session.commit()
    draft = await new_draft(session, user)  # version 1, step description

    with pytest.raises(DraftChanged):
        await set_priority(
            session, draft_id=draft.id, user_id=user.id, expected_version=1, priority=Priority.high
        )

    f = await fresh_draft(session, draft.id)
    assert f.version == 1
    assert f.step is DraftStep.description
    assert f.priority is None


async def test_deleted_draft_raises_draft_not_found(session: AsyncSession) -> None:
    user = await mk_user(session, 1001, Role.support)
    await session.commit()
    draft = await new_draft(session, user)

    assert await delete_draft(session, draft_id=draft.id, user_id=user.id) is True
    await session.commit()
    assert await get_draft(session, draft.id) is None

    with pytest.raises(DraftNotFound):
        await save_description(
            session, draft_id=draft.id, user_id=user.id, expected_version=1, description="desc"
        )


async def test_foreign_draft_id_raises_draft_not_found(session: AsyncSession) -> None:
    owner = await mk_user(session, 1001, Role.support)
    other = await mk_user(session, 1002, Role.support)
    await session.commit()
    draft = await new_draft(session, owner)
    draft = await save_description(
        session, draft_id=draft.id, user_id=owner.id, expected_version=1, description="desc"
    )
    await session.commit()  # version 2, step priority

    # Another user mutates the draft with the *correct* version: the foreign id is
    # indistinguishable from a missing draft, so it raises DraftNotFound (no leak).
    with pytest.raises(DraftNotFound):
        await set_priority(
            session,
            draft_id=draft.id,
            user_id=other.id,
            expected_version=2,
            priority=Priority.high,
        )

    f = await fresh_draft(session, draft.id)
    assert f.user_id == owner.id
    assert f.version == 2
    assert f.step is DraftStep.priority
    assert f.priority is None


async def test_set_prompt_message_id_no_version_bump_and_stale_step(session: AsyncSession) -> None:
    user = await mk_user(session, 1001, Role.support)
    await session.commit()
    draft = await new_draft(session, user)

    assert (
        await set_prompt_message_id(
            session,
            draft_id=draft.id,
            expected_version=1,
            expected_step=DraftStep.description,
            message_id=123,
        )
        is True
    )
    await session.commit()

    f = await fresh_draft(session, draft.id)
    assert f.prompt_message_id == 123
    assert f.version == 1  # no bump

    # Advancing clears the prompt and bumps the version.
    draft = await save_description(
        session, draft_id=draft.id, user_id=user.id, expected_version=1, description="desc"
    )
    await session.commit()
    assert draft.prompt_message_id is None
    assert draft.version == 2

    # A late prompt-send with the stale step is a no-op.
    late = await set_prompt_message_id(
        session,
        draft_id=draft.id,
        expected_version=2,
        expected_step=DraftStep.description,
        message_id=999,
    )
    assert late is False
    f2 = await fresh_draft(session, draft.id)
    assert f2.prompt_message_id is None


async def test_concurrent_save_description_exactly_one_succeeds(
    session: AsyncSession, engine: AsyncEngine
) -> None:
    user = await mk_user(session, 1001, Role.support)
    await session.commit()
    draft = await new_draft(session, user)
    draft_id = draft.id

    # Two independent sessions race to save the description at the same version:
    # exactly one wins (bumping version 1 -> 2) and the other raises DraftChanged.
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def do_save(s: AsyncSession) -> TicketDraft:
        result = await save_description(
            s, draft_id=draft_id, user_id=user.id, expected_version=1, description="one"
        )
        await s.commit()
        return result

    async with factory() as s1, factory() as s2:
        results = await asyncio.gather(do_save(s1), do_save(s2), return_exceptions=True)

    ok = [r for r in results if isinstance(r, TicketDraft)]
    changed = [r for r in results if isinstance(r, DraftChanged)]
    assert len(ok) == 1
    assert len(changed) == 1

    f = await fresh_draft(session, draft_id)
    assert f.version == 2
    assert f.description == "one"


async def test_concurrent_confirm_one_ticket(session: AsyncSession, engine: AsyncEngine) -> None:
    user = await mk_user(session, 1001, Role.support)
    await session.commit()
    draft = await new_draft(session, user)
    draft = await fill_draft(session, draft, tenant_id="tenant-1")  # version 5, step confirm
    await session.commit()
    draft_id = draft.id

    # Two independent sessions race to confirm the same draft. Running both in
    # ``asyncio.gather`` means each reads the draft before either DELETE commits,
    # so the loser fails the guarded claim with ``DraftChanged`` rather than
    # ``DraftNotFound`` (its fresh read still saw the uncommitted row).
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def do_confirm(s: AsyncSession) -> Ticket:
        ticket = await confirm(s, draft_id=draft_id, expected_version=5, author=user)
        await s.commit()
        return ticket

    async with factory() as s1, factory() as s2:
        results = await asyncio.gather(do_confirm(s1), do_confirm(s2), return_exceptions=True)

    tickets = [r for r in results if isinstance(r, Ticket)]
    errors = [r for r in results if isinstance(r, DraftChanged)]
    assert len(tickets) == 1
    assert len(errors) == 1

    assert cast(int, await session.scalar(select(func.count()).select_from(Ticket))) == 1
    # The draft row is gone from the DB (the ``session`` fixture still caches the
    # old object in its identity map, so read the column fresh).
    assert await session.scalar(select(TicketDraft.id).where(TicketDraft.id == draft_id)) is None


async def test_confirm_stale_version_raises_draft_changed(session: AsyncSession) -> None:
    user = await mk_user(session, 1001, Role.support)
    await session.commit()
    draft = await new_draft(session, user)
    draft = await fill_draft(session, draft, tenant_id="tenant-1")  # version 5, step confirm
    await session.commit()

    with pytest.raises(DraftChanged):
        await confirm(session, draft_id=draft.id, expected_version=4, author=user)

    assert cast(int, await session.scalar(select(func.count()).select_from(Ticket))) == 0
    f = await fresh_draft(session, draft.id)
    assert f.version == 5
    assert f.step is DraftStep.confirm


async def test_confirm_wrong_step_raises_draft_changed(session: AsyncSession) -> None:
    user = await mk_user(session, 1001, Role.support)
    await session.commit()
    draft = await new_draft(session, user)  # version 1, step description

    with pytest.raises(DraftChanged):
        await confirm(session, draft_id=draft.id, expected_version=1, author=user)

    assert cast(int, await session.scalar(select(func.count()).select_from(Ticket))) == 0
    f = await fresh_draft(session, draft.id)
    assert f.version == 1
    assert f.step is DraftStep.description


async def test_confirm_foreign_author_raises_draft_not_found(session: AsyncSession) -> None:
    owner = await mk_user(session, 1001, Role.support)
    other = await mk_user(session, 1002, Role.support)
    await session.commit()
    draft = await new_draft(session, owner)
    draft = await fill_draft(session, draft, tenant_id="tenant-1")  # version 5, step confirm
    await session.commit()

    with pytest.raises(DraftNotFound):
        await confirm(session, draft_id=draft.id, expected_version=5, author=other)

    assert cast(int, await session.scalar(select(func.count()).select_from(Ticket))) == 0
    f = await fresh_draft(session, draft.id)
    assert f.version == 5
    assert f.step is DraftStep.confirm
    assert f.user_id == owner.id


async def test_confirm_rolls_back_when_create_ticket_raises(
    session: AsyncSession, engine: AsyncEngine
) -> None:
    roleless = await mk_user(session, 1001)  # no role
    await session.commit()
    draft = await new_draft(session, roleless)
    draft = await fill_draft(session, draft, tenant_id="tenant-1")  # version 5, step confirm
    await session.commit()
    draft_id = draft.id

    # The draft service does not enforce role, so a roleless user can fill a draft;
    # only ``create_ticket`` (inside ``confirm``) rejects them.
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as fresh:
        with pytest.raises(PermissionDenied):
            await confirm(fresh, draft_id=draft_id, expected_version=5, author=roleless)
        await fresh.rollback()

    # The guarded DELETE rolled back with the transaction: no draft is deleted
    # without a ticket.
    f = await fresh_draft(session, draft_id)
    assert f.version == 5
    assert f.step is DraftStep.confirm
    assert cast(int, await session.scalar(select(func.count()).select_from(Ticket))) == 0


async def test_cleanup_expired_drafts(session: AsyncSession) -> None:
    u1 = await mk_user(session, 1001, Role.support)
    u2 = await mk_user(session, 1002, Role.support)
    await session.commit()
    old_draft = await new_draft(session, u1)
    fresh_draft = await new_draft(session, u2)

    now = datetime.now(UTC)
    # Age the first draft beyond the TTL.
    await session.execute(
        update(TicketDraft)
        .where(TicketDraft.id == old_draft.id)
        .values(created_at=now - timedelta(days=2))
    )
    await session.commit()

    removed = await cleanup_expired_drafts(session, now=now, ttl_seconds=86400)
    await session.commit()

    assert removed == 1
    assert await get_draft(session, old_draft.id) is None
    assert await get_draft(session, fresh_draft.id) is not None


async def test_cleanup_expired_drafts_boundary_not_deleted(session: AsyncSession) -> None:
    u1 = await mk_user(session, 1001, Role.support)
    u2 = await mk_user(session, 1002, Role.support)
    await session.commit()
    at_cutoff = await new_draft(session, u1)
    older = await new_draft(session, u2)

    now = datetime.now(UTC)
    ttl = 86400
    # Exactly at the cutoff is NOT deleted (strictly less-than); slightly older IS.
    await session.execute(
        update(TicketDraft)
        .where(TicketDraft.id == at_cutoff.id)
        .values(created_at=now - timedelta(seconds=ttl))
    )
    await session.execute(
        update(TicketDraft)
        .where(TicketDraft.id == older.id)
        .values(created_at=now - timedelta(seconds=ttl + 1))
    )
    await session.commit()

    removed = await cleanup_expired_drafts(session, now=now, ttl_seconds=ttl)
    await session.commit()

    assert removed == 1
    assert await get_draft(session, at_cutoff.id) is not None
    assert await get_draft(session, older.id) is None
