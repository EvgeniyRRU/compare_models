from uuid import uuid4

from app.bot.drafts import draft_callback, parse_draft_callback, save_prompt_if_current
from app.db.models import TicketDraft, User


def test_draft_callback_round_trips_compact_id_and_version() -> None:
    draft_id = uuid4()
    value = draft_callback("continue", draft_id, 17)
    assert len(value.encode()) <= 64
    assert parse_draft_callback(value) == ("continue", draft_id, 17)


def test_invalid_draft_callback_is_rejected() -> None:
    assert parse_draft_callback("d:submit:not-an-id:1") is None


async def test_late_prompt_does_not_replace_prompt_for_a_newer_step(db_session):
    from uuid import uuid4

    user = User(telegram_id=991, full_name="Draft test")
    db_session.add(user)
    await db_session.flush()
    draft = TicketDraft(
        id=uuid4(), user_id=user.id, step="request_id", version=3, prompt_message_id=500
    )
    db_session.add(draft)
    await db_session.flush()
    assert not await save_prompt_if_current(
        db_session, draft_id=draft.id, version=2, step="description", message_id=501
    )
    await db_session.refresh(draft)
    assert draft.prompt_message_id == 500


async def test_draft_callback_rechecks_current_role(db_session, monkeypatch):
    from types import SimpleNamespace

    from app.bot import drafts
    from app.db.models import Role

    user = User(telegram_id=992, full_name="Demoted user", role=Role.DEVELOPER)
    db_session.add(user)
    await db_session.flush()
    draft = TicketDraft(id=uuid4(), user_id=user.id, step="priority", version=4)
    db_session.add(draft)
    await db_session.flush()

    class ExistingSession:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(drafts, "SessionLocal", SimpleNamespace(begin=lambda: ExistingSession()))

    class Query:
        data = draft_callback("priority_high", draft.id, draft.version)

        class Message:
            async def reply_text(self, *args, **kwargs):
                return SimpleNamespace(message_id=501)

        message = Message()

        async def answer(self, text="", show_alert=False):
            self.answer_text = text
            self.show_alert = show_alert

    query = Query()
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(
            id=user.telegram_id, username=None, full_name=user.full_name
        ),
    )
    await drafts.draft_callback_handler(update, SimpleNamespace())

    await db_session.refresh(draft)
    assert draft.step == "priority"
    assert draft.version == 4
    assert draft.priority is None
    assert query.show_alert is True


async def test_replayed_text_reply_cannot_fill_the_following_draft_step(db_session, monkeypatch):
    from types import SimpleNamespace

    from app.bot import drafts
    from app.db.models import Priority, Role

    user = User(telegram_id=993, full_name="Support", role=Role.SUPPORT)
    db_session.add(user)
    await db_session.flush()
    draft = TicketDraft(
        id=uuid4(),
        user_id=user.id,
        step="request_id",
        description="Problem",
        priority=Priority.HIGH,
        version=2,
        prompt_message_id=501,
    )
    db_session.add(draft)
    await db_session.flush()

    class ExistingSession:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(drafts, "SessionLocal", SimpleNamespace(begin=lambda: ExistingSession()))

    class Message:
        reply_to_message = SimpleNamespace(message_id=501)
        text = "REQ-1"

        def __init__(self):
            self.next_message_id = 600

        async def reply_text(self, *args, **kwargs):
            self.next_message_id += 1
            return SimpleNamespace(message_id=self.next_message_id)

    message = Message()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(
            id=user.telegram_id, username=None, full_name=user.full_name
        ),
        effective_message=message,
        effective_chat=SimpleNamespace(type="private"),
    )
    await drafts.draft_text(update, SimpleNamespace())
    await db_session.refresh(draft)
    assert draft.step == "tenant_id"
    assert draft.request_id == "REQ-1"
    assert draft.version == 3
    current_prompt_id = draft.prompt_message_id

    await drafts.draft_text(update, SimpleNamespace())
    await db_session.refresh(draft)
    assert draft.step == "tenant_id"
    assert draft.request_id == "REQ-1"
    assert draft.tenant_id is None
    assert draft.version == 3
    assert draft.prompt_message_id != current_prompt_id
