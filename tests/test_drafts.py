import asyncio
import uuid

import pytest
from sqlalchemy import func, select, text

from support_bot.db.engine import SessionFactory
from support_bot.db.models import Ticket, TicketDraft
from support_bot.domain.enums import DraftStep, Priority, Role
from support_bot.domain.errors import PermissionDeniedError
from support_bot.services.drafts import (
    DraftNotFoundError,
    DraftService,
    DraftSnapshot,
    DraftStaleError,
    TextOutcome,
)
from support_bot.services.views import UserInfo
from tests.conftest import MakeUser


class Form:
    """Помощник: имитирует бота, который задаёт вопросы и сохраняет prompt_message_id."""

    def __init__(self, drafts: DraftService, user: UserInfo) -> None:
        self.drafts = drafts
        self.user = user
        self.msg = 100

    async def ask(self, d: DraftSnapshot) -> int:
        self.msg += 1
        await self.drafts.save_prompt(d.id, d.version, d.step, self.msg)
        return self.msg

    async def start(self) -> DraftSnapshot:
        res = await self.drafts.start(self.user.id)
        await self.ask(res.draft)
        return res.draft

    async def answer(self, text: str, reply_to: int | None = None) -> DraftSnapshot:
        res = await self.drafts.submit_text(self.user.id, reply_to or self.msg, text)
        assert res.outcome is TextOutcome.ACCEPTED, res
        await self.ask(res.draft)
        return res.draft


async def db_draft(sf: SessionFactory, user_id: int) -> TicketDraft | None:
    async with sf() as s:
        return (
            await s.execute(select(TicketDraft).where(TicketDraft.user_id == user_id))
        ).scalar_one_or_none()


async def test_full_form_creates_ticket(
    sf: SessionFactory, drafts: DraftService, make_user: MakeUser
) -> None:
    support = await make_user(Role.SUPPORT)
    f = Form(drafts, support)
    d = await f.start()
    assert (d.step, d.version) == (DraftStep.DESCRIPTION, 1)
    d = await f.answer("  Не проходит оплата  ")
    assert (d.step, d.version, d.description) == (DraftStep.PRIORITY, 2, "Не проходит оплата")
    d = await drafts.choose_priority(support.id, d.id, d.version, Priority.CRITICAL)
    await f.ask(d)
    d = await f.answer("REQ-42")
    assert d.step is DraftStep.TENANT_ID
    d = await drafts.skip_tenant(support.id, d.id, d.version)
    assert (d.step, d.tenant_id, d.version) == (DraftStep.CONFIRM, None, 5)
    tid = await drafts.submit(support.id, d.id, d.version)
    assert await db_draft(sf, support.id) is None
    async with sf() as s:
        t = (await s.execute(select(Ticket).where(Ticket.id == tid))).scalar_one()
    assert (t.description, t.priority, t.request_id, t.tenant_id) == (
        "Не проходит оплата",
        Priority.CRITICAL,
        "REQ-42",
        None,
    )
    # повторное «Отправить» ничего не создаёт
    with pytest.raises(DraftNotFoundError):
        await drafts.submit(support.id, d.id, d.version)
    async with sf() as s:
        assert await s.scalar(select(func.count()).select_from(Ticket)) == 1


async def test_developer_cannot_create(drafts: DraftService, make_user: MakeUser) -> None:
    dev = await make_user(Role.DEVELOPER)
    with pytest.raises(PermissionDeniedError):
        await drafts.start(dev.id)


async def test_validation_does_not_change_draft(
    sf: SessionFactory, drafts: DraftService, make_user: MakeUser
) -> None:
    support = await make_user(Role.SUPPORT)
    f = Form(drafts, support)
    d = await f.start()
    for bad in ("   ", "x" * 3001):
        res = await drafts.submit_text(support.id, f.msg, bad)
        assert res.outcome is TextOutcome.INVALID and res.error
    row = await db_draft(sf, support.id)
    assert row is not None
    assert (row.step, row.version, row.description) == (d.step, 1, None)


async def test_unbound_messages_do_not_change_draft(
    sf: SessionFactory, drafts: DraftService, make_user: MakeUser
) -> None:
    support = await make_user(Role.SUPPORT)
    f = Form(drafts, support)
    await f.start()
    prompt = f.msg
    for reply_to in (None, prompt - 1, 9999):
        res = await drafts.submit_text(support.id, reply_to, "текст")
        assert res.outcome is TextOutcome.UNBOUND
    row = await db_draft(sf, support.id)
    assert row is not None
    assert (row.step, row.version, row.description, row.prompt_message_id) == (
        DraftStep.DESCRIPTION,
        1,
        None,
        prompt,
    )


async def test_text_on_button_step_is_unbound(
    sf: SessionFactory, drafts: DraftService, make_user: MakeUser
) -> None:
    support = await make_user(Role.SUPPORT)
    f = Form(drafts, support)
    await f.start()
    await f.answer("описание")  # шаг priority, вопрос сохранён
    res = await drafts.submit_text(support.id, f.msg, "high")
    assert res.outcome is TextOutcome.UNBOUND
    row = await db_draft(sf, support.id)
    assert row is not None and (row.step, row.version) == (DraftStep.PRIORITY, 2)


async def test_resent_answer_does_not_leak_into_next_field(
    sf: SessionFactory, drafts: DraftService, make_user: MakeUser
) -> None:
    """Ответ сохранён, но бот упал до отправки следующего вопроса; пользователь повторил."""
    support = await make_user(Role.SUPPORT)
    f = Form(drafts, support)
    d = await f.start()
    d = await drafts.choose_priority(support.id, *(await _to_priority(f, "описание")), Priority.LOW)
    await f.ask(d)
    first_prompt = f.msg
    res = await drafts.submit_text(support.id, first_prompt, "REQ-1")
    assert res.outcome is TextOutcome.ACCEPTED  # следующий вопрос не отправлен (сбой)
    # Повтор того же ответа (reply на старый вопрос) не попадает в поле арендатора.
    res = await drafts.submit_text(support.id, first_prompt, "REQ-1")
    assert res.outcome is TextOutcome.UNBOUND
    row = await db_draft(sf, support.id)
    assert row is not None
    assert (row.step, row.tenant_id, row.prompt_message_id) == (DraftStep.TENANT_ID, None, None)


async def _to_priority(f: Form, description: str) -> tuple[uuid.UUID, int]:
    d = await f.answer(description)
    return d.id, d.version


async def test_concurrent_answers_only_one_accepted(
    sf: SessionFactory, drafts: DraftService, make_user: MakeUser
) -> None:
    support = await make_user(Role.SUPPORT)
    f = Form(drafts, support)
    await f.start()
    results = await asyncio.gather(
        *(drafts.submit_text(support.id, f.msg, f"ответ {i}") for i in range(5))
    )
    assert sum(r.outcome is TextOutcome.ACCEPTED for r in results) == 1
    assert sum(r.outcome is TextOutcome.UNBOUND for r in results) == 4
    row = await db_draft(sf, support.id)
    assert row is not None and row.version == 2 and row.step is DraftStep.PRIORITY


async def test_concurrent_priority_clicks(
    sf: SessionFactory, drafts: DraftService, make_user: MakeUser
) -> None:
    support = await make_user(Role.SUPPORT)
    f = Form(drafts, support)
    await f.start()
    d = await f.answer("описание")
    results = await asyncio.gather(
        drafts.choose_priority(support.id, d.id, d.version, Priority.HIGH),
        drafts.choose_priority(support.id, d.id, d.version, Priority.LOW),
        return_exceptions=True,
    )
    assert sum(isinstance(r, DraftStaleError) for r in results) == 1
    row = await db_draft(sf, support.id)
    assert row is not None and row.version == 3


async def test_buttons_from_deleted_draft_cannot_confirm_new(
    sf: SessionFactory, drafts: DraftService, make_user: MakeUser
) -> None:
    support = await make_user(Role.SUPPORT)
    f = Form(drafts, support)
    old = await f.start()
    old = await f.answer("старое")
    old = await drafts.choose_priority(support.id, old.id, old.version, Priority.HIGH)
    await f.ask(old)
    old = await f.answer("REQ-OLD")
    old = await drafts.skip_tenant(support.id, old.id, old.version)
    assert old.step is DraftStep.CONFIRM

    # Пользователь начинает заново: старый черновик удалён, новый проходит до confirm.
    new = await drafts.restart(support.id, old.id, old.version)
    await f.ask(new)
    new = await f.answer("новое")
    new = await drafts.choose_priority(support.id, new.id, new.version, Priority.LOW)
    await f.ask(new)
    new = await f.answer("REQ-NEW")
    new = await drafts.skip_tenant(support.id, new.id, new.version)
    assert new.version == old.version  # совпадение версий не помогает: другой id

    with pytest.raises(DraftStaleError) as exc_info:
        await drafts.submit(support.id, old.id, old.version)
    assert exc_info.value.current is not None and exc_info.value.current.id == new.id
    with pytest.raises(DraftStaleError):
        await drafts.cancel_by_button(support.id, old.id, old.version)
    async with sf() as s:
        assert await s.scalar(select(func.count()).select_from(Ticket)) == 0


async def test_stale_version_button_rejected(
    sf: SessionFactory, drafts: DraftService, make_user: MakeUser
) -> None:
    support = await make_user(Role.SUPPORT)
    f = Form(drafts, support)
    await f.start()
    d = await f.answer("описание")
    d2 = await drafts.choose_priority(support.id, d.id, d.version, Priority.HIGH)
    with pytest.raises(DraftStaleError) as exc_info:
        await drafts.choose_priority(support.id, d.id, d.version, Priority.LOW)
    assert exc_info.value.current == d2
    row = await db_draft(sf, support.id)
    assert row is not None and row.priority is Priority.HIGH and row.version == d2.version


async def test_late_prompt_does_not_overwrite_next_step(
    sf: SessionFactory, drafts: DraftService, make_user: MakeUser
) -> None:
    support = await make_user(Role.SUPPORT)
    f = Form(drafts, support)
    d1 = await f.start()
    res = await drafts.submit_text(support.id, f.msg, "описание")
    d2 = res.draft
    assert await drafts.save_prompt(d2.id, d2.version, d2.step, 500)
    # Запоздавшая отправка повторного вопроса шага 1 завершилась только сейчас.
    assert not await drafts.save_prompt(d1.id, d1.version, d1.step, 777)
    row = await db_draft(sf, support.id)
    assert row is not None and row.prompt_message_id == 500 and row.version == 2


async def test_save_prompt_does_not_bump_version(
    sf: SessionFactory, drafts: DraftService, make_user: MakeUser
) -> None:
    support = await make_user(Role.SUPPORT)
    d = (await drafts.start(support.id)).draft
    assert await drafts.save_prompt(d.id, d.version, d.step, 1)
    assert await drafts.save_prompt(d.id, d.version, d.step, 2)
    row = await db_draft(sf, support.id)
    assert row is not None and (row.version, row.prompt_message_id) == (1, 2)


async def test_new_with_existing_offers_continue(drafts: DraftService, make_user: MakeUser) -> None:
    support = await make_user(Role.SUPPORT)
    first = await drafts.start(support.id)
    again = await drafts.start(support.id)
    assert again.existing and again.draft.id == first.draft.id
    resumed = await drafts.resume(support.id, first.draft.id, first.draft.version)
    assert resumed == first.draft


async def test_cancel_and_ttl(
    sf: SessionFactory, drafts: DraftService, make_user: MakeUser
) -> None:
    support = await make_user(Role.SUPPORT)
    await drafts.start(support.id)
    assert await drafts.cancel(support.id)
    assert not await drafts.cancel(support.id)

    d = (await drafts.start(support.id)).draft
    async with sf.begin() as s:
        await s.execute(
            text("ALTER TABLE ticket_drafts DISABLE TRIGGER trg_ticket_drafts_updated_at")
        )
        await s.execute(text("UPDATE ticket_drafts SET updated_at = now() - interval '25 hours'"))
        await s.execute(
            text("ALTER TABLE ticket_drafts ENABLE TRIGGER trg_ticket_drafts_updated_at")
        )
    # просроченный черновик не активен: /new создаёт новый
    res = await drafts.start(support.id)
    assert not res.existing and res.draft.id != d.id

    async with sf.begin() as s:
        await s.execute(
            text("ALTER TABLE ticket_drafts DISABLE TRIGGER trg_ticket_drafts_updated_at")
        )
        await s.execute(text("UPDATE ticket_drafts SET updated_at = now() - interval '25 hours'"))
        await s.execute(
            text("ALTER TABLE ticket_drafts ENABLE TRIGGER trg_ticket_drafts_updated_at")
        )
    assert await drafts.delete_expired() == 1
