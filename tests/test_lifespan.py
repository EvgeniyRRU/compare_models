"""Tests for the FastAPI lifespan wiring (webhook path, fake PTB application)."""

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

import app.background as background
from app.config import settings
from app.main import create_app


class FakeBot:
    """Minimal ``Bot`` stand-in: records ``set_webhook``; sends are no-ops."""

    def __init__(self) -> None:
        self.set_webhook_calls: list[dict[str, Any]] = []

    async def set_webhook(self, *, url: str, secret_token: str | None = None) -> bool:
        self.set_webhook_calls.append({"url": url, "secret_token": secret_token})
        return True

    async def send_message(
        self, *, chat_id: int, text: str, reply_to_message_id: int | None = None
    ) -> None:
        return None

    async def edit_message_text(self, **kwargs: Any) -> None:
        return None


class FakeApplication:
    """Minimal ``Application`` stand-in recording initialize/shutdown calls."""

    def __init__(self) -> None:
        self.bot = FakeBot()
        self.initialized = 0
        self.shutdown_count = 0

    async def initialize(self) -> None:
        self.initialized += 1

    async def shutdown(self) -> None:
        self.shutdown_count += 1


async def _noop(*args: Any, **kwargs: Any) -> None:
    return None


async def test_lifespan_webhook_initializes_sets_webhook_and_shuts_down(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "bot_mode", "webhook")
    monkeypatch.setattr(settings, "webhook_url", "https://example.com/hook")
    monkeypatch.setattr(settings, "webhook_secret", "")

    # Neutralize the four background passes so the spawned loops never touch
    # the database or Telegram while the lifespan is active.
    monkeypatch.setattr(background, "sync_pending", _noop)
    monkeypatch.setattr(background, "run_reminder_pass", _noop)
    monkeypatch.setattr(background, "cleanup_expired_drafts", _noop)
    monkeypatch.setattr(background, "cleanup_old_updates", _noop)

    disposed = False

    async def spy_dispose(self: AsyncEngine) -> None:
        nonlocal disposed
        disposed = True

    monkeypatch.setattr(AsyncEngine, "dispose", spy_dispose)

    fake_app = FakeApplication()
    app = create_app(engine=engine, application=fake_app)  # type: ignore[arg-type]

    async with app.router.lifespan_context(app):
        assert app.state.application is fake_app
        assert fake_app.initialized == 1
        assert fake_app.bot.set_webhook_calls == [
            {"url": "https://example.com/hook", "secret_token": None}
        ]

    assert fake_app.shutdown_count == 1
    assert disposed is True
