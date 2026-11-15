"""Ticket service: creation, transitions, versions, journal, concurrency races."""

import asyncio

import pytest
from sqlalchemy import select, text, update

from app.config import settings
from app.db.models import Priority, Role, Ticket, TicketEvent, TicketStatus, User
from app.domain.errors import (
    AlreadyTaken,
    Forbidden,
    InvalidTransition,
    NotFound,
    ValidationError,
    VersionConflict,
)
from app.domain.state_machine import Action
from app.services import tickets

NEW, PROG, WAIT = TicketStatus.new, TicketStatus.in_progress, TicketStatus.waiting_info
RES = TicketStatus.resolved

SUPPORT_ID = 500
DEV_ID = 101
DEV2_ID = 102
ADMIN_ID = 1


async def mk_user(session, telegram_id: int, role: Role | None) -> User:
    user = User(
        telegram_id=telegram_id,
        username=f"u{telegram_id}",
        full_name=f"User {telegram_id}",
        role=role,
    )
    session.add(user)
    await session.commit()
    return user


async def mk_ticket(session, description="Сломалась стиральная машина", priority="high") -> Ticket:
    return await tickets.create(
        session,
        settings,
        author_id=SUPPORT_ID,
        description=description,
        priority=priority,
        request_id="REQ-1",
        tenant_id="T-9",
    )


def _reminder_fresh_sql(priority: Priority) -> str:
    minutes = settings.reminder_interval_minutes(priority)
    return (
        "SELECT next_reminder_at >= now() - interval '2 minutes' "
        f"AND next_reminder_at <= now() + make_interval(mins => {minutes} + 2) "
        "FROM tickets WHERE id = :id"
    )


async def reminder_is_fresh(session, ticket_id: int, priority: Priority) -> bool:
    return bool(await session.scalar(text(_reminder_fresh_sql(priority)), {"id": ticket_id}))


async def journal(session, ticket_id: int) -> list[TicketEvent]:
    rows = await session.scalars(
        select(TicketEvent).where(TicketEvent.ticket_id == ticket_id).order_by(TicketEvent.id)
    )
    return list(rows)


class TestCreation:
    async def test_create_sets_fields_reminder_and_first_event(self, db_session):
        await mk_user(db_session, SUPPORT_ID, Role.support)
        t = await mk_ticket(db_session, priority="critical")
        await db_session.commit()
        assert t.status is NEW and t.version == 1
        assert t.assignee_id is None and t.taken_at is None and t.closed_at is None
        assert t.request_id == "REQ-1" and t.tenant_id == "T-9"
        assert t.priority is Priority.critical
        assert await reminder_is_fresh(db_session, t.id, Priority.critical)
        evs = await journal(db_session, t.id)
        assert len(evs) == 1
        assert evs[0].event_type == "created" and evs[0].version_after == 1
        assert await tickets.last_new_entry_at(db_session, t.id) is not None

    @pytest.mark.parametrize(
        ("description", "request_id", "tenant_id"),
        [
            ("", "REQ-1", None),
            ("x" * 3001, "REQ-1", None),
            ("норм", "", None),
            ("норм", "r" * 65, None),
            ("норм", "REQ-1", "t" * 65),
        ],
    )
    async def test_create_validation_rejects(self, db_session, description, request_id, tenant_id):
        with pytest.raises(ValidationError):
            await tickets.create(
                db_session,
                settings,
                author_id=SUPPORT_ID,
                description=description,
                priority="high",
                request_id=request_id,
                tenant_id=tenant_id,
            )

    async def test_create_unknown_priority_rejected(self, db_session):
        with pytest.raises(ValidationError):
            await tickets.create(
                db_session,
                settings,
                author_id=SUPPORT_ID,
                description="x",
                priority="urgent",
                request_id="REQ-1",
                tenant_id=None,
            )

    async def test_ticket_ids_are_unique_and_sequential(self, db_session):
        await mk_user(db_session, SUPPORT_ID, Role.support)
        a = await mk_ticket(db_session, "раз")
        b = await mk_ticket(db_session, "два")
        await db_session.commit()
        assert b.id > a.id


class TestTake:
    async def test_take_by_developer_success(self, db_session):
        await mk_user(db_session, SUPPORT_ID, Role.support)
        await mk_user(db_session, DEV_ID, Role.developer)
        t = await mk_ticket(db_session)
        await db_session.commit()
        got = await tickets.take(
            db_session, settings, ticket_id=t.id, actor_id=DEV_ID, expected_version=1
        )
        await db_session.commit()
        assert got.status is PROG
        assert got.assignee_id == DEV_ID and got.taken_at is not None
        assert got.next_reminder_at is None
        assert got.version == 2
        evs = await journal(db_session, t.id)
        assert [e.event_type for e in evs] == ["created", "taken"]
        assert evs[1].old_status is NEW and evs[1].new_status is PROG
        assert evs[1].new_assignee_id == DEV_ID and evs[1].version_after == 2

    async def test_take_by_admin_success(self, db_session):
        await mk_user(db_session, SUPPORT_ID, Role.support)
        await mk_user(db_session, ADMIN_ID, Role.admin)
        t = await mk_ticket(db_session)
        await db_session.commit()
        got = await tickets.take(
            db_session, settings, ticket_id=t.id, actor_id=ADMIN_ID, expected_version=1
        )
        assert got.assignee_id == ADMIN_ID

    @pytest.mark.parametrize("role", [Role.support, None])
    async def test_take_requires_executor_role(self, db_session, role):
        await mk_user(db_session, SUPPORT_ID, Role.support)
        await mk_user(db_session, DEV_ID, role)
        t = await mk_ticket(db_session)
        await db_session.commit()
        with pytest.raises(Forbidden):
            await tickets.take(
                db_session, settings, ticket_id=t.id, actor_id=DEV_ID, expected_version=1
            )

    async def test_second_take_is_already_taken(self, db_session):
        await mk_user(db_session, SUPPORT_ID, Role.support)
        await mk_user(db_session, DEV_ID, Role.developer)
        await mk_user(db_session, DEV2_ID, Role.developer)
        t = await mk_ticket(db_session)
        await db_session.commit()
        await tickets.take(
            db_session, settings, ticket_id=t.id, actor_id=DEV_ID, expected_version=1
        )
        await db_session.commit()
        with pytest.raises(AlreadyTaken):
            await tickets.take(
                db_session, settings, ticket_id=t.id, actor_id=DEV2_ID, expected_version=1
            )

    async def test_take_missing_ticket_not_found(self, db_session):
        await mk_user(db_session, DEV_ID, Role.developer)
        with pytest.raises(NotFound):
            await tickets.take(
                db_session, settings, ticket_id=424_242, actor_id=DEV_ID, expected_version=1
            )


class TestLifecycleAndVersions:
    async def _taken(self, db_session) -> Ticket:
        await mk_user(db_session, SUPPORT_ID, Role.support)
        await mk_user(db_session, DEV_ID, Role.developer)
        t = await mk_ticket(db_session)
        await db_session.commit()
        t = await tickets.take(
            db_session, settings, ticket_id=t.id, actor_id=DEV_ID, expected_version=1
        )
        await db_session.commit()
        return t

    async def test_stale_version_rejected_with_message(self, db_session):
        t = await self._taken(db_session)
        with pytest.raises(VersionConflict) as exc:
            await tickets.apply_action(
                db_session,
                settings,
                ticket_id=t.id,
                actor_id=DEV_ID,
                action=Action.to_waiting,
                expected_version=1,
            )
        assert "Обращение изменилось" in str(exc.value.message)

    async def test_full_chain_journal_and_versions(self, db_session):
        t = await self._taken(db_session)
        t = await tickets.apply_action(
            db_session,
            settings,
            ticket_id=t.id,
            actor_id=DEV_ID,
            action=Action.to_waiting,
            expected_version=2,
        )
        assert t.status is WAIT and t.version == 3
        t = await tickets.apply_action(
            db_session,
            settings,
            ticket_id=t.id,
            actor_id=DEV_ID,
            action=Action.to_progress,
            expected_version=3,
        )
        assert t.status is PROG and t.version == 4
        t = await tickets.apply_action(
            db_session,
            settings,
            ticket_id=t.id,
            actor_id=DEV_ID,
            action=Action.resolve,
            expected_version=4,
        )
        await db_session.commit()
        assert t.status is RES and t.version == 5 and t.closed_at is not None
        evs = await journal(db_session, t.id)
        assert [e.version_after for e in evs] == [1, 2, 3, 4, 5]
        assert [e.event_type for e in evs] == [
            "created",
            "taken",
            "status_changed",
            "status_changed",
            "status_changed",
        ]
        assert evs[4].old_status is PROG and evs[4].new_status is RES

    async def test_stranger_developer_cannot_move_others_ticket(self, db_session):
        t = await self._taken(db_session)
        await mk_user(db_session, DEV2_ID, Role.developer)
        with pytest.raises(Forbidden):
            await tickets.apply_action(
                db_session,
                settings,
                ticket_id=t.id,
                actor_id=DEV2_ID,
                action=Action.resolve,
                expected_version=2,
            )

    async def test_reject_from_new_invalid(self, db_session):
        await mk_user(db_session, SUPPORT_ID, Role.support)
        await mk_user(db_session, ADMIN_ID, Role.admin)
        t = await mk_ticket(db_session)
        await db_session.commit()
        with pytest.raises(InvalidTransition):
            await tickets.apply_action(
                db_session,
                settings,
                ticket_id=t.id,
                actor_id=ADMIN_ID,
                action=Action.reject,
                expected_version=1,
            )


class TestAbandonReopenReassign:
    async def test_abandon_resets_and_rearms_reminder(self, db_session):
        await mk_user(db_session, SUPPORT_ID, Role.support)
        await mk_user(db_session, DEV_ID, Role.developer)
        t = await mk_ticket(db_session, priority="high")
        await db_session.commit()
        t = await tickets.take(
            db_session, settings, ticket_id=t.id, actor_id=DEV_ID, expected_version=1
        )
        await db_session.commit()
        t = await tickets.apply_action(
            db_session,
            settings,
            ticket_id=t.id,
            actor_id=DEV_ID,
            action=Action.abandon,
            expected_version=t.version,
        )
        await db_session.commit()
        assert t.status is NEW and t.assignee_id is None and t.taken_at is None
        assert await reminder_is_fresh(db_session, t.id, Priority.high)
        evs = await journal(db_session, t.id)
        assert evs[-1].event_type == "abandoned"
        assert evs[-1].old_assignee_id == DEV_ID and evs[-1].new_assignee_id is None

    async def test_reopen_admin_only_resets_closed_at(self, db_session):
        await mk_user(db_session, SUPPORT_ID, Role.support)
        await mk_user(db_session, DEV_ID, Role.developer)
        await mk_user(db_session, ADMIN_ID, Role.admin)
        t = await mk_ticket(db_session)
        await db_session.commit()
        t = await tickets.take(
            db_session, settings, ticket_id=t.id, actor_id=DEV_ID, expected_version=1
        )
        t = await tickets.apply_action(
            db_session,
            settings,
            ticket_id=t.id,
            actor_id=DEV_ID,
            action=Action.resolve,
            expected_version=2,
        )
        await db_session.commit()
        with pytest.raises(Forbidden):
            await tickets.apply_action(
                db_session,
                settings,
                ticket_id=t.id,
                actor_id=DEV_ID,
                action=Action.reopen,
                expected_version=t.version,
            )
        t = await tickets.apply_action(
            db_session,
            settings,
            ticket_id=t.id,
            actor_id=ADMIN_ID,
            action=Action.reopen,
            expected_version=t.version,
        )
        await db_session.commit()
        assert t.status is NEW and t.closed_at is None and t.assignee_id is None
        assert await reminder_is_fresh(db_session, t.id, Priority.high)
        assert (await journal(db_session, t.id))[-1].event_type == "reopened"
        # and it can be taken again
        t = await tickets.take(
            db_session, settings, ticket_id=t.id, actor_id=DEV_ID, expected_version=t.version
        )
        assert t.status is PROG

    async def test_reassign_admin_only_target_and_status_kept(self, db_session):
        await mk_user(db_session, SUPPORT_ID, Role.support)
        await mk_user(db_session, DEV_ID, Role.developer)
        await mk_user(db_session, DEV2_ID, Role.developer)
        await mk_user(db_session, ADMIN_ID, Role.admin)
        t = await mk_ticket(db_session)
        await db_session.commit()
        t = await tickets.take(
            db_session, settings, ticket_id=t.id, actor_id=DEV_ID, expected_version=1
        )
        await db_session.commit()
        with pytest.raises(Forbidden):
            await tickets.apply_action(
                db_session,
                settings,
                ticket_id=t.id,
                actor_id=DEV_ID,
                action=Action.reassign,
                expected_version=t.version,
                target_assignee_id=DEV2_ID,
            )
        t = await tickets.apply_action(
            db_session,
            settings,
            ticket_id=t.id,
            actor_id=ADMIN_ID,
            action=Action.reassign,
            expected_version=t.version,
            target_assignee_id=DEV2_ID,
        )
        await db_session.commit()
        assert t.status is PROG and t.assignee_id == DEV2_ID and t.taken_at is not None
        ev = (await journal(db_session, t.id))[-1]
        assert ev.event_type == "reassigned"
        assert ev.old_assignee_id == DEV_ID and ev.new_assignee_id == DEV2_ID
        assert ev.old_status is PROG and ev.new_status is PROG

    async def test_reassign_to_non_executor_rejected(self, db_session):
        await mk_user(db_session, SUPPORT_ID, Role.support)
        await mk_user(db_session, DEV_ID, Role.developer)
        await mk_user(db_session, ADMIN_ID, Role.admin)
        t = await mk_ticket(db_session)
        await db_session.commit()
        t = await tickets.take(
            db_session, settings, ticket_id=t.id, actor_id=DEV_ID, expected_version=1
        )
        await db_session.commit()
        with pytest.raises(ValidationError):
            await tickets.apply_action(
                db_session,
                settings,
                ticket_id=t.id,
                actor_id=ADMIN_ID,
                action=Action.reassign,
                expected_version=t.version,
                target_assignee_id=SUPPORT_ID,
            )


class TestConcurrency:
    async def test_two_concurrent_takes_exactly_one_wins(self, session_factory):
        async with session_factory() as s:
            await mk_user(s, SUPPORT_ID, Role.support)
            await mk_user(s, DEV_ID, Role.developer)
            await mk_user(s, DEV2_ID, Role.developer)
            t = await mk_ticket(s)
            await s.commit()
            ticket_id = t.id

        async def do_take(actor: int):
            async with session_factory() as s, s.begin():
                return await tickets.take(
                    s, settings, ticket_id=ticket_id, actor_id=actor, expected_version=1
                )

        results = await asyncio.gather(do_take(DEV_ID), do_take(DEV2_ID), return_exceptions=True)
        wins = [r for r in results if isinstance(r, Ticket)]
        losses = [r for r in results if isinstance(r, AlreadyTaken)]
        assert len(wins) == 1
        assert len(losses) == 1, f"expected one AlreadyTaken, got {results!r}"
        async with session_factory() as s:
            ticket = await s.get(Ticket, ticket_id)
            assert ticket.status is PROG and ticket.version == 2

    async def test_take_vs_demotion_race_denied_under_lock(self, session_factory):
        """Role stripped concurrently while take is in flight: FOR SHARE re-read must deny."""
        async with session_factory() as s:
            await mk_user(s, SUPPORT_ID, Role.support)
            await mk_user(s, DEV_ID, Role.developer)
            t = await mk_ticket(s)
            await s.commit()
            ticket_id = t.id

        blocker = session_factory()
        tx = await blocker.begin()
        await blocker.execute(select(User).where(User.telegram_id == DEV_ID).with_for_update())

        async def do_take():
            async with session_factory() as s, s.begin():
                return await tickets.take(
                    s, settings, ticket_id=ticket_id, actor_id=DEV_ID, expected_version=1
                )

        task = asyncio.create_task(do_take())
        await asyncio.sleep(0.3)  # let take reach the FOR SHARE wait
        await blocker.execute(
            update(User).where(User.telegram_id == DEV_ID).values(role=Role.support)
        )
        await tx.commit()
        try:
            with pytest.raises(Forbidden):
                await task
        finally:
            await blocker.close()
        async with session_factory() as s:
            ticket = await s.get(Ticket, ticket_id)
            assert ticket.status is NEW and ticket.assignee_id is None
