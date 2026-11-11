import socket
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from support_bot import main
from support_bot.bot.app import build_application
from support_bot.config import Settings
from support_bot.web.app import create_app
from tests.fake_telegram import FakeTelegram


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.parametrize("mode", ["polling", "webhook"])
async def test_lifespan_starts_and_stops_in_both_modes(
    mode: str, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTelegram()
    settings.bot_mode = mode  # type: ignore[assignment]
    settings.webhook_url = "https://example.test/telegram/webhook"
    settings.webhook_secret_token = SecretStr("s3cret")
    settings.mini_app_url = "https://example.test/app/"
    settings.metrics_port = _free_port()
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(
        main,
        "build_application",
        lambda s, e, ss: build_application(s, e, ss, request=fake),
    )
    app = create_app(lifespan=main.lifespan)

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            ready: Any = await http.get("/health/ready")

    assert ready.status_code == 200
    names = [name for name, _ in fake.calls]
    assert "setChatMenuButton" in names
    if mode == "webhook":
        assert "setWebhook" in names
    else:
        assert "deleteWebhook" in names
