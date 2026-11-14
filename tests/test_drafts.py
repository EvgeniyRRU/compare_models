from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update

from support_bot.db.enums import DraftStep, TicketPriority, UserRole
from support_bot.db.models.ticket import Ticket
from support_bot.db.models.ticket_draft import TicketDraft
from support_bot.db.models.user import User
from support_bot.domain.errors import (
    DraftConflict,
    DraftValidationError,
    NoActiveDraft,
    PermissionDenied,
)
from support_bot.services import drafts as service


async def _create(session_factory, user_id: int):
    async with session_factory() as session:
        snapshot, created = await service.create_or_get(session, user_id)
    assert created is True
    return snapshot


async def _prompt(session_factory, snapshot, message_id: int):
    async with session_factory() as session:
        saved = await service.set_prompt_message_id(
            session,
            draft_id=snapshot.id,
            version=snapshot.version,
            step=snapshot.step,
            message_id=message_id,
        )
    assert saved is True


async def _text(session_factory, user_id: int, text: str, message_id: int | None):
    async with session_factory() as session:
        return await service.submit_text(
            session, user_id=user_id, text=text, reply_to_message_id=message_id
        )


async def _load(session_factory, draft_id: uuid.UUID) -> TicketDraft | None:
    async with session_factory() as session:
        return await session.get(TicketDraft, draft_id)


async def test_create_or_get_is_unique_per_user(session_factory, make_user) -> None:
    user = await make_user(telegram_id=400, role=UserRole.SUPPORT)
    first = await _create(session_factory, user.id)
    async with session_factory() as session:
        second, created = await service.create_or_get(session, user.id)
    assert created is False
    assert second.id == first.id
    async with session_factory() as session:
        count = await session.scalar(
            select(func.count()).select_from(TicketDraft).where(TicketDraft.user_id == user.id)
        )
    assert count == 1


async def test_text_requires_reply_to_current_prompt(session_factory, make_user) -> None:
    user = await make_user(telegram_id=401, role=UserRole.SUPPORT)
    snapshot = await _create(session_factory, user.id)
    await _prompt(session_factory, snapshot, 100)

    for wrong in (None, 99):
        async with session_factory() as session:
            try:
                await service.submit_text(
                    session, user_id=user.id, text="описание", reply_to_message_id=wrong
                )
            except DraftConflict:
                pass
            else:
                raise AssertionError("unpaired message must be rejected")

    draft = await _load(session_factory, snapshot.id)
    assert draft is not None
    assert draft.version == 1
    assert draft.step is DraftStep.DESCRIPTION
    assert draft.description is None

    accepted = await _text(session_factory, user.id, "  описание  ", 100)
    assert accepted.version == 2
    assert accepted.step is DraftStep.PRIORITY
    assert accepted.description == "описание"
    assert accepted.prompt_message_id is None


async def test_validation_does_not_change_version(session_factory, make_user) -> None:
    user = await make_user(telegram_id=402, role=UserRole.SUPPORT)
    snapshot = await _create(session_factory, user.id)
    await _prompt(session_factory, snapshot, 100)
    async with session_factory() as session:
        try:
            await service.submit_text(session, user_id=user.id, text="   ", reply_to_message_id=100)
        except DraftValidationError:
            pass
        else:
            raise AssertionError("empty text must be rejected")
    draft = await _load(session_factory, snapshot.id)
    assert draft is not None
    assert draft.version == 1
    assert draft.step is DraftStep.DESCRIPTION


async def test_concurrent_text_updates_only_one_wins(session_factory, make_user) -> None:
    user = await make_user(telegram_id=403, role=UserRole.SUPPORT)
    snapshot = await _create(session_factory, user.id)
    await _prompt(session_factory, snapshot, 100)

    async def attempt(text: str):
        async with session_factory() as session:
            return await service.submit_text(
                session, user_id=user.id, text=text, reply_to_message_id=100
            )

    results = await asyncio.gather(attempt("первое"), attempt("второе"), return_exceptions=True)
    successes = [result for result in results if not isinstance(result, Exception)]
    conflicts = [result for result in results if isinstance(result, DraftConflict)]
    assert len(successes) == 1
    assert len(conflicts) == 1

    draft = await _load(session_factory, snapshot.id)
    assert draft is not None
    assert draft.version == 2
    assert draft.description == successes[0].description
    assert draft.step is DraftStep.PRIORITY


async def test_stale_and_foreign_callbacks_are_rejected(session_factory, make_user) -> None:
    user = await make_user(telegram_id=404, role=UserRole.SUPPORT)
    other = await make_user(telegram_id=405, role=UserRole.SUPPORT)
    snapshot = await _create(session_factory, user.id)
    other_snapshot = await _create(session_factory, other.id)

    async with session_factory() as session:
        try:
            await service.choose_priority(
                session,
                user_id=user.id,
                draft_id=snapshot.id,
                version=99,
                priority=TicketPriority.HIGH,
            )
        except DraftConflict:
            pass
        else:
            raise AssertionError("stale version must be rejected")

    async with session_factory() as session:
        try:
            await service.choose_priority(
                session,
                user_id=user.id,
                draft_id=other_snapshot.id,
                version=other_snapshot.version,
                priority=TicketPriority.HIGH,
            )
        except DraftConflict:
            pass
        else:
            raise AssertionError("foreign draft must be rejected")

    draft = await _load(session_factory, snapshot.id)
    assert draft is not None
    assert draft.version == 1
    assert draft.priority is None


async def test_delayed_prompt_does_not_overwrite_next_step(session_factory, make_user) -> None:
    user = await make_user(telegram_id=406, role=UserRole.SUPPORT)
    snapshot = await _create(session_factory, user.id)
    await _prompt(session_factory, snapshot, 100)
    await _text(session_factory, user.id, "описание", 100)

    async with session_factory() as session:
        stale = await service.set_prompt_message_id(
            session,
            draft_id=snapshot.id,
            version=1,
            step=DraftStep.DESCRIPTION,
            message_id=777,
        )
    assert stale is False

    draft = await _load(session_factory, snapshot.id)
    assert draft is not None
    assert draft.step is DraftStep.PRIORITY
    assert draft.prompt_message_id is None

    async with session_factory() as session:
        fresh = await service.set_prompt_message_id(
            session,
            draft_id=snapshot.id,
            version=2,
            step=DraftStep.PRIORITY,
            message_id=888,
        )
    assert fresh is True
    draft = await _load(session_factory, snapshot.id)
    assert draft is not None
    assert draft.prompt_message_id == 888


async def test_full_form_creates_ticket_and_deletes_draft(
    session_factory, make_user, policy
) -> None:
    user = await make_user(telegram_id=407, role=UserRole.SUPPORT)
    snapshot = await _create(session_factory, user.id)
    await _prompt(session_factory, snapshot, 100)
    snapshot = await _text(session_factory, user.id, "Не работает вход", 100)
    assert snapshot.step is DraftStep.PRIORITY

    async with session_factory() as session:
        snapshot = await service.choose_priority(
            session,
            user_id=user.id,
            draft_id=snapshot.id,
            version=snapshot.version,
            priority=TicketPriority.CRITICAL,
        )
    await _prompt(session_factory, snapshot, 101)
    snapshot = await _text(session_factory, user.id, "REQ-42", 101)
    assert snapshot.step is DraftStep.TENANT_ID

    async with session_factory() as session:
        snapshot = await service.skip_tenant(
            session,
            user_id=user.id,
            draft_id=snapshot.id,
            version=snapshot.version,
        )
    assert snapshot.step is DraftStep.CONFIRM
    async with session_factory() as session:
        ticket = await service.confirm(
            session,
            user_id=user.id,
            draft_id=snapshot.id,
            version=snapshot.version,
            policy=policy,
        )
    assert ticket.description == "Не работает вход"
    assert ticket.priority is TicketPriority.CRITICAL
    assert ticket.request_id == "REQ-42"
    assert ticket.tenant_id is None
    assert ticket.author_id == user.id

    assert await _load(session_factory, snapshot.id) is None


async def test_confirm_twice_creates_one_ticket(session_factory, make_user, policy) -> None:
    user = await make_user(telegram_id=408, role=UserRole.SUPPORT)
    snapshot = await _confirmed_snapshot(session_factory, user.id)

    async with session_factory() as session:
        await service.confirm(
            session,
            user_id=user.id,
            draft_id=snapshot.id,
            version=snapshot.version,
            policy=policy,
        )
    async with session_factory() as session:
        try:
            await service.confirm(
                session,
                user_id=user.id,
                draft_id=snapshot.id,
                version=snapshot.version,
                policy=policy,
            )
        except DraftConflict:
            pass
        else:
            raise AssertionError("second confirm must be rejected")
    async with session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(Ticket))
    assert count == 1


async def test_confirm_requires_role_at_submit_time(session_factory, make_user, policy) -> None:
    user = await make_user(telegram_id=409, role=UserRole.SUPPORT)
    snapshot = await _confirmed_snapshot(session_factory, user.id)
    async with session_factory() as session, session.begin():
        await session.execute(update(User).where(User.id == user.id).values(role=None))
    async with session_factory() as session:
        try:
            await service.confirm(
                session,
                user_id=user.id,
                draft_id=snapshot.id,
                version=snapshot.version,
                policy=policy,
            )
        except PermissionDenied:
            pass
        else:
            raise AssertionError("role is required")

    draft = await _load(session_factory, snapshot.id)
    assert draft is not None


async def test_restart_requires_current_version(session_factory, make_user) -> None:
    user = await make_user(telegram_id=410, role=UserRole.SUPPORT)
    snapshot = await _create(session_factory, user.id)
    async with session_factory() as session:
        try:
            await service.restart(
                session,
                user_id=user.id,
                expected_draft_id=snapshot.id,
                expected_version=99,
            )
        except DraftConflict:
            pass
        else:
            raise AssertionError("stale restart must be rejected")
    assert await _load(session_factory, snapshot.id) is not None

    async with session_factory() as session:
        restarted = await service.restart(
            session,
            user_id=user.id,
            expected_draft_id=snapshot.id,
            expected_version=snapshot.version,
        )
    assert restarted.id != snapshot.id
    assert restarted.version == 1
    assert await _load(session_factory, snapshot.id) is None


async def test_expired_draft_is_removed(session_factory, make_user) -> None:
    user = await make_user(telegram_id=411, role=UserRole.SUPPORT)
    snapshot = await _create(session_factory, user.id)
    async with session_factory() as session, session.begin():
        await session.execute(
            update(TicketDraft)
            .where(TicketDraft.id == snapshot.id)
            .values(updated_at=datetime.now(UTC) - timedelta(hours=48))
        )
    async with session_factory() as session:
        assert await service.existing_active(session, user.id, ttl_hours=24) is None
    assert await _load(session_factory, snapshot.id) is None


async def test_submit_without_draft_raises(session_factory, make_user) -> None:
    user = await make_user(telegram_id=412, role=UserRole.SUPPORT)
    async with session_factory() as session:
        try:
            await service.submit_text(session, user_id=user.id, text="x", reply_to_message_id=1)
        except NoActiveDraft:
            pass
        else:
            raise AssertionError("draft is required")


async def _confirmed_snapshot(session_factory, user_id: int):
    snapshot = await _create(session_factory, user_id)
    await _prompt(session_factory, snapshot, 100)
    snapshot = await _text(session_factory, user_id, "Описание", 100)
    async with session_factory() as session:
        snapshot = await service.choose_priority(
            session,
            user_id=user_id,
            draft_id=snapshot.id,
            version=snapshot.version,
            priority=TicketPriority.MEDIUM,
        )
    await _prompt(session_factory, snapshot, 101)
    snapshot = await _text(session_factory, user_id, "REQ-1", 101)
    async with session_factory() as session:
        snapshot = await service.skip_tenant(
            session,
            user_id=user_id,
            draft_id=snapshot.id,
            version=snapshot.version,
        )
    return snapshot
