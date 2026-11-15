"""Role administration: admin-only, demotion safety, executor search."""

import pytest

from app.config import settings
from app.db.models import Role, Ticket, User
from app.domain.errors import Forbidden, NotFound, RoleChangeBlocked
from app.domain.state_machine import Action
from app.services import roles, tickets

ADMIN_ID = 1
DEV_ID = 101
DEV2_ID = 102
SUPPORT_ID = 500


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


async def mk_ticket(session) -> Ticket:
    return await tickets.create(
        session,
        settings,
        author_id=SUPPORT_ID,
        description="Протечка на кухне",
        priority="high",
        request_id="REQ-1",
        tenant_id=None,
    )


class TestSetRole:
    async def test_non_admin_actor_forbidden(self, db_session):
        await mk_user(db_session, DEV_ID, Role.developer)
        await mk_user(db_session, DEV2_ID, None)
        with pytest.raises(Forbidden):
            await roles.set_role(db_session, DEV_ID, DEV2_ID, Role.developer)

    async def test_unknown_target_not_found(self, db_session):
        await mk_user(db_session, ADMIN_ID, Role.admin)
        with pytest.raises(NotFound):
            await roles.set_role(db_session, ADMIN_ID, 999_999, Role.developer)

    async def test_admin_grants_and_revokes_roles(self, db_session):
        await mk_user(db_session, ADMIN_ID, Role.admin)
        await mk_user(db_session, DEV2_ID, None)
        u = await roles.set_role(db_session, ADMIN_ID, DEV2_ID, Role.developer)
        assert u.role is Role.developer
        u = await roles.set_role(db_session, ADMIN_ID, DEV2_ID, Role.support)
        assert u.role is Role.support
        u = await roles.set_role(db_session, ADMIN_ID, DEV2_ID, None)
        assert u.role is None
        await db_session.commit()

    async def test_demotion_blocked_while_active_tickets(self, db_session):
        await mk_user(db_session, SUPPORT_ID, Role.support)
        await mk_user(db_session, DEV_ID, Role.developer)
        await mk_user(db_session, ADMIN_ID, Role.admin)
        t = await mk_ticket(db_session)
        await db_session.commit()
        t = await tickets.take(
            db_session, settings, ticket_id=t.id, actor_id=DEV_ID, expected_version=1
        )
        await db_session.commit()
        for new_role in (Role.support, None):
            with pytest.raises(RoleChangeBlocked) as exc:
                await roles.set_role(db_session, ADMIN_ID, DEV_ID, new_role)
            assert list(exc.value.active_ticket_ids) == [t.id]
        # waiting counts as active too
        await tickets.apply_action(
            db_session,
            settings,
            ticket_id=t.id,
            actor_id=DEV_ID,
            action=Action.to_waiting,
            expected_version=t.version,
        )
        await db_session.commit()
        with pytest.raises(RoleChangeBlocked):
            await roles.set_role(db_session, ADMIN_ID, DEV_ID, Role.support)

    async def test_demotion_allowed_after_closure_or_reassign(self, db_session):
        await mk_user(db_session, SUPPORT_ID, Role.support)
        await mk_user(db_session, DEV_ID, Role.developer)
        await mk_user(db_session, DEV2_ID, Role.developer)
        await mk_user(db_session, ADMIN_ID, Role.admin)
        t = await mk_ticket(db_session)
        await db_session.commit()
        await tickets.take(
            db_session, settings, ticket_id=t.id, actor_id=DEV_ID, expected_version=1
        )
        await db_session.commit()
        # reassign away -> free to demote
        await tickets.apply_action(
            db_session,
            settings,
            ticket_id=t.id,
            actor_id=ADMIN_ID,
            action=Action.reassign,
            expected_version=2,
            target_assignee_id=DEV2_ID,
        )
        await db_session.commit()
        u = await roles.set_role(db_session, ADMIN_ID, DEV_ID, Role.support)
        assert u.role is Role.support

    async def test_admin_to_developer_keeps_executor_rights(self, db_session):
        await mk_user(db_session, ADMIN_ID, Role.admin)
        await mk_user(db_session, DEV_ID, Role.admin)
        u = await roles.set_role(db_session, ADMIN_ID, DEV_ID, Role.developer)
        assert u.role is Role.developer

    async def test_revoke_admin_with_active_tickets_blocked(self, db_session):
        await mk_user(db_session, SUPPORT_ID, Role.support)
        await mk_user(db_session, ADMIN_ID, Role.admin)
        t = await mk_ticket(db_session)
        await db_session.commit()
        await tickets.take(
            db_session, settings, ticket_id=t.id, actor_id=ADMIN_ID, expected_version=1
        )
        await db_session.commit()
        with pytest.raises(RoleChangeBlocked):
            await roles.set_role(db_session, ADMIN_ID, ADMIN_ID, None)


class TestExecutorSearch:
    async def test_lists_only_executors_case_insensitive(self, db_session):
        for tid, role, name in [
            (DEV_ID, Role.developer, "Petr Ivanov"),
            (DEV2_ID, Role.admin, "peter_admin"),
            (SUPPORT_ID, Role.support, "Petr Supportov"),
            (777, None, "Petr NoRole"),
        ]:
            session_user = User(
                telegram_id=tid, username=name.lower().replace(" ", "_"), full_name=name, role=role
            )
            db_session.add(session_user)
        await db_session.commit()
        found = await roles.users_with_executor_role(db_session, "pet")
        assert {u.telegram_id for u in found} == {DEV_ID, DEV2_ID}
        everyone = await roles.users_with_executor_role(db_session)
        assert {u.telegram_id for u in everyone} == {DEV_ID, DEV2_ID}
