import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from support_bot.db.models import Ticket, TicketEvent
from support_bot.domain.errors import BusinessError, Forbidden
from support_bot.services.drafts import DraftService
from tests.integration.test_tickets import actors

pytestmark = pytest.mark.integration


async def answer(service, user_id, draft, value, message_id=100):
    assert await service.save_prompt(draft.id, draft.version, draft.step, message_id)
    return await service.text(user_id, message_id, value)


async def confirm(service, user_id):
    d = await service.begin(user_id)
    d = await answer(service, user_id, d, "Description")
    assert d.step == "priority" and d.version == 2 and d.prompt_message_id is None
    d = await service.action(user_id, d.id, d.version, "phigh")
    d = await answer(service, user_id, d, "REQUEST")
    d = await service.action(user_id, d.id, d.version, "skip")
    assert d.step == "confirm" and d.version == 5
    return d


async def test_draft_submit_is_atomic_and_repeated_submit_does_not_duplicate(db, settings):
    _, _, support, _, _ = await actors(db, settings)
    service = DraftService(db.sessions, settings)
    d = await confirm(service, support.id)
    results = await asyncio.gather(
        *[service.action(support.id, d.id, d.version, "submit") for _ in range(2)],
        return_exceptions=True,
    )
    assert sum(isinstance(r, Ticket) for r in results) == 1
    assert sum(isinstance(r, BusinessError) for r in results) == 1
    assert await service.get(support.id) is None
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Ticket)) == 1
        assert await session.scalar(select(func.count()).select_from(TicketEvent)) == 1


@pytest.mark.parametrize("reply_id", [None, 99])
async def test_text_requires_exact_force_reply_binding(db, settings, reply_id):
    _, _, support, _, _ = await actors(db, settings)
    service = DraftService(db.sessions, settings)
    d = await service.begin(support.id)
    await service.save_prompt(d.id, d.version, d.step, 100)
    with pytest.raises(BusinessError) as error:
        await service.text(support.id, reply_id, "Response")
    assert error.value.code == "stale_draft"
    current = await service.get(support.id)
    assert current.version == 1 and current.description is None
    assert current.prompt_message_id == 100 and current.step == "description"


async def test_draft_stale_uuid_version_and_delayed_prompt_never_overwrite(db, settings):
    _, _, support, _, _ = await actors(db, settings)
    service = DraftService(db.sessions, settings)
    old = await service.begin(support.id)
    d = await service.action(support.id, old.id, old.version, "restart")
    assert d.id != old.id and d.version == 1
    for draft_id, version in [(old.id, 1), (uuid4(), 1), (d.id, 0)]:
        with pytest.raises(BusinessError) as error:
            await service.action(support.id, draft_id, version, "cancel")
        assert error.value.code == "stale_draft"
    assert not await service.save_prompt(old.id, 1, "description", 90)
    d = await answer(service, support.id, d, "D")
    assert not await service.save_prompt(d.id, 1, "description", 90)
    assert not await service.save_prompt(d.id, d.version, "description", 91)
    current = await service.get(support.id)
    assert current.step == "priority" and current.prompt_message_id is None
    with pytest.raises(BusinessError):
        await service.text(support.id, 100, "Repeated answer")
    continued = await service.action(support.id, d.id, d.version, "continue")
    assert continued.version == d.version


async def test_draft_validation_cancel_and_tenant_input(db, settings):
    _, _, support, _, _ = await actors(db, settings)
    service = DraftService(db.sessions, settings)
    d = await service.begin(support.id)
    await service.save_prompt(d.id, 1, "description", 100)
    for invalid in ["", " ", "x" * 3001]:
        with pytest.raises(BusinessError):
            await service.text(support.id, 100, invalid)
        assert (await service.get(support.id)).version == 1
    d = await service.text(support.id, 100, "D")
    for invalid in ["purgent", "skip", "submit"]:
        with pytest.raises(BusinessError):
            await service.action(support.id, d.id, d.version, invalid)
    d = await service.action(support.id, d.id, d.version, "plow")
    await service.save_prompt(d.id, d.version, d.step, 101)
    for invalid in [" ", "x" * 65]:
        with pytest.raises(BusinessError):
            await service.text(support.id, 101, invalid)
    d = await service.text(support.id, 101, "R")
    d = await answer(service, support.id, d, "TENANT", 102)
    assert d.tenant_id == "TENANT" and d.step == "confirm"
    assert await service.action(support.id, d.id, d.version, "cancel") is None
    assert await service.get(support.id) is None
    d = await service.begin(support.id)
    await service.cancel(support.id)
    assert await service.get(support.id) is None


async def test_parallel_begin_ttl_and_current_role_checks(db, settings):
    users, admin, support, developer, _ = await actors(db, settings)
    service = DraftService(db.sessions, settings)
    first, second = await asyncio.gather(service.begin(support.id), service.begin(support.id))
    assert first.id == second.id
    # The production trigger always timestamps UPDATE, so arrange expiry using a short TTL.
    settings.draft_ttl_hours = 1e-9
    assert await service.get(support.id) is None
    settings.draft_ttl_hours = 24
    d = await service.begin(support.id)
    assert d.id != first.id
    await users.set_role(admin.id, support.id, None)
    for operation in [
        service.get(support.id),
        service.begin(support.id),
        service.text(support.id, 1, "D"),
        service.action(support.id, d.id, d.version, "submit"),
        service.cancel(support.id),
        service.begin(developer.id),
    ]:
        with pytest.raises(Forbidden):
            await operation


async def test_submit_validation_failure_preserves_draft_and_has_no_ticket_event(db, settings):
    from sqlalchemy import update

    from support_bot.db.models import TicketDraft

    _, _, support, _, _ = await actors(db, settings)
    service = DraftService(db.sessions, settings)
    d = await confirm(service, support.id)
    async with db.sessions.begin() as session:
        await session.execute(
            update(TicketDraft).where(TicketDraft.id == d.id).values(priority="bad")
        )
    with pytest.raises(BusinessError):
        await service.action(support.id, d.id, d.version, "submit")
    current = await service.get(support.id)
    assert current.id == d.id and current.version == d.version and current.step == "confirm"
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Ticket)) == 0
        assert await session.scalar(select(func.count()).select_from(TicketEvent)) == 0


async def test_foreign_user_and_empty_binding_cannot_modify_draft(db, settings):
    _, admin, support, _, _ = await actors(db, settings)
    service = DraftService(db.sessions, settings)
    d = await service.begin(support.id)
    other = await service.begin(admin.id)
    with pytest.raises(BusinessError) as error:
        await service.text(support.id, 1, "D")
    assert error.value.code == "stale_draft"
    with pytest.raises(BusinessError):
        await service.action(admin.id, d.id, d.version, "cancel")
    assert (await service.get(admin.id)).id == other.id
    assert (await service.get(support.id)).version == 1
    settings.draft_ttl_hours = 1e-9
    assert not await service.save_prompt(d.id, d.version, d.step, 100)
    settings.draft_ttl_hours = 24
    await service.cancel(support.id)
    replacement = await service.begin(support.id)
    with pytest.raises(BusinessError):
        await service.action(support.id, d.id, d.version, "submit")
    assert (await service.get(support.id)).id == replacement.id


async def test_concurrent_text_answers_advance_only_once(db, settings):
    _, _, support, _, _ = await actors(db, settings)
    service = DraftService(db.sessions, settings)
    draft = await service.begin(support.id)
    await service.save_prompt(draft.id, draft.version, draft.step, 100)
    results = await asyncio.gather(
        service.text(support.id, 100, "first answer"),
        service.text(support.id, 100, "second answer"),
        return_exceptions=True,
    )
    assert sum(isinstance(result, BusinessError) for result in results) == 1
    current = await service.get(support.id)
    assert current.version == 2 and current.step == "priority"
    assert current.description in {"first answer", "second answer"}
    assert current.request_id is None and current.prompt_message_id is None
    current = await service.action(support.id, current.id, current.version, "phigh")
    await service.save_prompt(current.id, current.version, current.step, 101)
    with pytest.raises(BusinessError):
        await service.text(support.id, 100, "retried old answer")
    saved = await service.get(support.id)
    assert saved.version == 3 and saved.request_id is None
