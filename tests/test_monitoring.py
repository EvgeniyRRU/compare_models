"""Tests for :mod:`app.monitoring`."""

import pytest
import sentry_sdk

import app.monitoring as monitoring
from app.config import settings


def test_init_sentry_noop_without_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``sentry_dsn`` is None, ``sentry_sdk.init`` is never called."""
    monkeypatch.setattr(settings, "sentry_dsn", None)
    init_calls: list[dict[str, object]] = []

    def fake_init(**kwargs: object) -> None:
        init_calls.append(kwargs)

    monkeypatch.setattr(sentry_sdk, "init", fake_init)

    monitoring.init_sentry()

    assert init_calls == []


def test_init_sentry_noop_with_empty_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``sentry_dsn`` is an empty string, ``sentry_sdk.init`` is never called."""
    monkeypatch.setattr(settings, "sentry_dsn", "")
    init_calls: list[dict[str, object]] = []

    def fake_init(**kwargs: object) -> None:
        init_calls.append(kwargs)

    monkeypatch.setattr(sentry_sdk, "init", fake_init)

    monitoring.init_sentry()

    assert init_calls == []


def test_init_sentry_called_with_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``sentry_dsn`` is set, ``sentry_sdk.init`` is called with it and the environment."""
    monkeypatch.setattr(settings, "sentry_dsn", "https://key@sentry.io/1")
    monkeypatch.setattr(settings, "sentry_environment", "staging")
    init_calls: list[dict[str, object]] = []

    def fake_init(**kwargs: object) -> None:
        init_calls.append(kwargs)

    monkeypatch.setattr(sentry_sdk, "init", fake_init)

    monitoring.init_sentry()

    assert len(init_calls) == 1
    assert init_calls[0]["dsn"] == "https://key@sentry.io/1"
    assert init_calls[0]["environment"] == "staging"
