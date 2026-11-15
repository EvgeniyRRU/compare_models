"""Tests for the background loop drivers (fake passes, no Telegram side effects)."""

import asyncio
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

import app.background as background
from app.db.session import make_session_factory


class _DummyPostClient:
    """Structural ``PostClient`` stand-in; never actually called by these tests."""

    async def send_post(self, text: str, reply_markup: Any | None = None) -> int:
        return 0

    async def edit_post(self, message_id: int, text: str, reply_markup: Any | None = None) -> int:
        return message_id


async def test_run_post_sync_loop_drives_sync_pending_once(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    stop = asyncio.Event()

    async def fake_sync_pending(**kwargs: Any) -> int:
        calls.append(kwargs)
        stop.set()
        return 1

    monkeypatch.setattr(background, "sync_pending", fake_sync_pending)

    await background.run_post_sync_loop(
        engine=engine,
        session_factory=make_session_factory(engine),
        client=_DummyPostClient(),
        interval=0.0,
        stop_event=stop,
    )

    assert len(calls) == 1


async def test_run_reminder_loop_drives_reminder_pass_once(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    stop = asyncio.Event()

    async def fake_reminder_pass(**kwargs: Any) -> int:
        calls.append(kwargs)
        stop.set()
        return 0

    monkeypatch.setattr(background, "run_reminder_pass", fake_reminder_pass)

    async def send_reminder(text: str, reply_to_message_id: int | None) -> None:
        return None

    await background.run_reminder_loop(
        session_factory=make_session_factory(engine),
        send_reminder=send_reminder,
        interval=0.0,
        stop_event=stop,
    )

    assert len(calls) == 1


async def test_run_draft_cleanup_loop_drives_cleanup_once(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    stop = asyncio.Event()

    async def fake_cleanup_expired_drafts(session: Any, **kwargs: Any) -> int:
        calls.append(kwargs)
        stop.set()
        return 0

    monkeypatch.setattr(background, "cleanup_expired_drafts", fake_cleanup_expired_drafts)

    await background.run_draft_cleanup_loop(
        session_factory=make_session_factory(engine),
        interval=0.0,
        stop_event=stop,
    )

    assert len(calls) == 1


async def test_run_processed_updates_cleanup_loop_drives_cleanup_once(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []
    stop = asyncio.Event()

    async def fake_cleanup_old_updates(session: Any) -> int:
        calls.append(session)
        stop.set()
        return 0

    monkeypatch.setattr(background, "cleanup_old_updates", fake_cleanup_old_updates)

    await background.run_processed_updates_cleanup_loop(
        session_factory=make_session_factory(engine),
        interval=0.0,
        stop_event=stop,
    )

    assert len(calls) == 1
