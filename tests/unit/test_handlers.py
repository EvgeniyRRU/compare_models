from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from telegram import ForceReply

from support_bot.db.models import Ticket, TicketDraft, User
from support_bot.domain.errors import BusinessError, Conflict
from support_bot.telegram.handlers import BotHandlers


@pytest.fixture
def bot_handlers(settings):
    users = SimpleNamespace(touch=AsyncMock())
    tickets = SimpleNamespace(get=AsyncMock(), change=AsyncMock())
    drafts = SimpleNamespace(
        get=AsyncMock(),
        begin=AsyncMock(),
        text=AsyncMock(),
        action=AsyncMock(),
        save_prompt=AsyncMock(),
        cancel=AsyncMock(),
    )
    posts = SimpleNamespace(sync=AsyncMock())
    return BotHandlers(settings, users, tickets, drafts, posts)


def update(*, text="", callback=None, private=True, reply=None):
    message = SimpleNamespace(
        text=text,
        reply_to_message=SimpleNamespace(message_id=reply) if reply else None,
        reply_text=AsyncMock(return_value=SimpleNamespace(message_id=99)),
    )
    query = (
        SimpleNamespace(data=callback, answer=AsyncMock(), message=message) if callback else None
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=123, first_name="Anna", username="anna", last_name=None),
        effective_chat=SimpleNamespace(
            type="private" if private else "supergroup", id=123 if private else -100123
        ),
        effective_message=message,
        callback_query=query,
    )


def context(role="support"):
    return SimpleNamespace(
        profile=User(id=7, telegram_id=123, role=role),
        bot=SimpleNamespace(set_chat_menu_button=AsyncMock()),
    )


async def test_profile_bootstrap_only_private_start(bot_handlers):
    await bot_handlers.profile(update(text="/new"), context())
    assert bot_handlers.users.touch.await_args.kwargs["bootstrap"] is False
    await bot_handlers.profile(update(text="/start"), context())
    assert bot_handlers.users.touch.await_args.kwargs["bootstrap"] is True
    await bot_handlers.profile(update(text="/start", private=False), context())
    assert bot_handlers.users.touch.await_args.kwargs["bootstrap"] is False


async def test_new_existing_draft_offers_guarded_continue_and_restart(bot_handlers):
    draft = TicketDraft(id=uuid4(), version=4, step="priority")
    bot_handlers.drafts.get.return_value = draft
    incoming = update(text="/new")
    await bot_handlers.new(incoming, context())
    buttons = incoming.effective_message.reply_text.await_args.kwargs[
        "reply_markup"
    ].inline_keyboard
    assert {button.callback_data for row in buttons for button in row} == {
        f"d:{draft.id.hex}:4:continue",
        f"d:{draft.id.hex}:4:restart",
    }
    bot_handlers.drafts.begin.assert_not_awaited()


async def test_tenant_prompt_sends_force_reply_and_separate_skip_then_saves_binding(bot_handlers):
    draft = TicketDraft(id=uuid4(), version=3, step="tenant_id")
    incoming = update()
    await bot_handlers.show_draft(incoming, draft)
    calls = incoming.effective_message.reply_text.await_args_list
    assert isinstance(calls[0].kwargs["reply_markup"], ForceReply)
    assert (
        calls[1].kwargs["reply_markup"].inline_keyboard[0][0].callback_data
        == f"d:{draft.id.hex}:3:skip"
    )
    bot_handlers.drafts.save_prompt.assert_awaited_once_with(draft.id, 3, "tenant_id", 99)


async def test_wrong_text_binding_redisplays_current_draft_without_advancing(bot_handlers):
    draft = TicketDraft(id=uuid4(), version=5, step="request_id")
    bot_handlers.drafts.text.side_effect = BusinessError("Форма изменилась", code="stale_draft")
    bot_handlers.drafts.get.return_value = draft
    incoming = update(text="late answer", reply=88)
    await bot_handlers.text(incoming, context())
    bot_handlers.drafts.text.assert_awaited_once_with(7, 88, "late answer")
    bot_handlers.drafts.save_prompt.assert_awaited_once_with(draft.id, 5, "request_id", 99)


async def test_stale_ticket_callback_alert_and_sync_after_service_failure(bot_handlers):
    bot_handlers.tickets.get.return_value = Ticket(id=12, version=3, status="in_progress")
    bot_handlers.tickets.change.side_effect = Conflict("Уже взято")
    incoming = update(callback="t:take:12:1", private=False)
    await bot_handlers.callback(incoming, context("developer"))
    bot_handlers.tickets.change.assert_awaited_once_with(
        actor_id=7, ticket_id=12, expected_version=1, expected_status="in_progress", action="take"
    )
    incoming.callback_query.answer.assert_awaited_once_with("Уже взято", show_alert=True)
    bot_handlers.posts.sync.assert_awaited_once_with(12)


async def test_draft_submit_publishes_committed_ticket_without_author_notification(bot_handlers):
    draft_id = uuid4()
    bot_handlers.drafts.action.return_value = Ticket(id=11)
    incoming = update(callback=f"d:{draft_id.hex}:5:submit")
    await bot_handlers.callback(incoming, context())
    bot_handlers.drafts.action.assert_awaited_once_with(7, draft_id, 5, "submit")
    bot_handlers.posts.sync.assert_awaited_once_with(11)
    assert "#11" in incoming.effective_message.reply_text.await_args.args[0]


async def test_unassigned_user_and_group_creation_cannot_begin_draft(bot_handlers):
    await bot_handlers.new(update(text="/new"), context(None))
    await bot_handlers.new(update(text="/new", private=False), context())
    bot_handlers.drafts.begin.assert_not_awaited()


async def test_old_draft_callback_redisplays_latest_uuid(bot_handlers):
    latest = TicketDraft(id=uuid4(), version=1, step="description")
    bot_handlers.drafts.action.side_effect = BusinessError("Форма изменилась", code="stale_draft")
    bot_handlers.drafts.get.return_value = latest
    incoming = update(callback=f"d:{uuid4().hex}:8:submit")
    await bot_handlers.callback(incoming, context())
    incoming.callback_query.answer.assert_awaited_once_with("Форма изменилась", show_alert=True)
    bot_handlers.drafts.save_prompt.assert_awaited_once_with(latest.id, 1, "description", 99)


async def test_nontext_update_still_touches_profile(bot_handlers):
    incoming = update()
    incoming.effective_message.text = None
    await bot_handlers.profile(incoming, context())
    bot_handlers.users.touch.assert_awaited_once()


async def test_take_loser_uses_service_conflict_after_new_snapshot(bot_handlers):
    bot_handlers.tickets.get.return_value = Ticket(id=12, version=1, status="new")
    bot_handlers.tickets.change.side_effect = Conflict("Уже взято")
    incoming = update(callback="t:take:12:1", private=False)
    await bot_handlers.callback(incoming, context("developer"))
    incoming.callback_query.answer.assert_awaited_once_with("Уже взято", show_alert=True)
