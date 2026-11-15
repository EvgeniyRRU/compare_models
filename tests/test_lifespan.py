"""Tests for the FastAPI lifespan wiring (webhook + polling, fake PTB application)."""

import asyncio
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


class FakeUpdater:
    """Minimal ``Updater`` stand-in recording start_polling/stop + running flag."""

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls
        self.running = False

    async def start_polling(self, allowed_updates: Any = None) -> None:
        self._calls.append("start_polling")
        self.running = True

    async def stop(self) -> None:
        self._calls.append("updater.stop")
        self.running = False


class FakeApplication:
    """Minimal ``Application`` stand-in recording the PTB lifecycle call order."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.bot = FakeBot()
        self.updater = FakeUpdater(self.calls)
        self.initialized = 0
        self.shutdown_count = 0
        self.running = False

    async def initialize(self) -> None:
        self.calls.append("initialize")
        self.initialized += 1

    async def start(self) -> None:
        self.calls.append("start")
        self.running = True

    async def stop(self) -> None:
        self.calls.append("stop")
        self.running = False

    async def shutdown(self) -> None:
        self.calls.append("shutdown")
        self.shutdown_count += 1


async def _noop(*args: Any, **kwargs: Any) -> None:
    return None


@pytest.fixture
def recorded_tasks(monkeypatch: pytest.MonkeyPatch) -> list[asyncio.Task[Any]]:
    """Record every ``asyncio.create_task`` call, delegating to the real one."""
    recorded: list[asyncio.Task[Any]] = []
    real_create_task = asyncio.create_task

    def recording_create_task(coro: Any, **kwargs: Any) -> asyncio.Task[Any]:
        task = real_create_task(coro, **kwargs)
        recorded.append(task)
        return task

    monkeypatch.setattr(asyncio, "create_task", recording_create_task)
    return recorded


def _neutralize_background_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the four background passes with no-ops (no DB/Telegram side effects)."""
    monkeypatch.setattr(background, "sync_pending", _noop)
    monkeypatch.setattr(background, "run_reminder_pass", _noop)
    monkeypatch.setattr(background, "cleanup_expired_drafts", _noop)
    monkeypatch.setattr(background, "cleanup_old_updates", _noop)


async def test_lifespan_webhook_initializes_sets_webhook_and_shuts_down(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    recorded_tasks: list[asyncio.Task[Any]],
) -> None:
    monkeypatch.setattr(settings, "bot_mode", "webhook")
    monkeypatch.setattr(settings, "webhook_url", "https://example.com/hook")
    monkeypatch.setattr(settings, "webhook_secret", "")

    _neutralize_background_passes(monkeypatch)

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
    assert len(recorded_tasks) >= 4
    assert all(task.done() for task in recorded_tasks)


async def test_lifespan_polling_branch(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    recorded_tasks: list[asyncio.Task[Any]],
) -> None:
    monkeypatch.setattr(settings, "bot_mode", "polling")

    _neutralize_background_passes(monkeypatch)

    disposed = False

    async def spy_dispose(self: AsyncEngine) -> None:
        nonlocal disposed
        disposed = True

    monkeypatch.setattr(AsyncEngine, "dispose", spy_dispose)

    fake_app = FakeApplication()
    app = create_app(engine=engine, application=fake_app)  # type: ignore[arg-type]

    async with app.router.lifespan_context(app):
        assert app.state.application is fake_app
        assert fake_app.calls == ["initialize", "start", "start_polling"]

    assert fake_app.calls == [
        "initialize",
        "start",
        "start_polling",
        "updater.stop",
        "stop",
        "shutdown",
    ]
    assert disposed is True
    assert len(recorded_tasks) >= 4
    assert all(task.done() for task in recorded_tasks)
