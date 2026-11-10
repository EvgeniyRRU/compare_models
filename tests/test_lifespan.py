from types import SimpleNamespace

import httpx

from app import main


async def test_application_lifecycle_and_webhook(sessions, monkeypatch):
    class FakeApp:
        def __init__(self):
            self.bot_data = {}
            self.bot = object()

        def add_handler(self, *args, **kwargs):
            pass

        def add_error_handler(self, *args):
            pass

        async def initialize(self):
            pass

        async def start(self):
            pass

        async def stop(self):
            pass

        async def shutdown(self):
            pass

        async def process_update(self, update):
            self.processed = update.update_id

    class Builder:
        def token(self, token):
            return self

        def updater(self, updater):
            return self

        def build(self):
            return fake_bot

    class Server:
        def shutdown(self):
            pass

        def server_close(self):
            pass

    fake_bot = FakeApp()
    settings = main.Settings(
        bot_token="123:abc",
        webhook_secret="secret",
        database_url="unused",
        dev_chat_id=-1,
        mini_app_url="https://example.org",
    )
    monkeypatch.setattr(main, "Settings", lambda: settings)
    monkeypatch.setattr(main, "create_database", lambda _: (sessions.kw["bind"], sessions))
    monkeypatch.setattr(main, "Application", SimpleNamespace(builder=lambda: Builder()))
    monkeypatch.setattr(main, "start_http_server", lambda port: (Server(), None))
    async with main.app.router.lifespan_context(main.app):
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/health/ready")).status_code == 200
            assert (await client.get("/")).status_code == 200
            assert (
                await client.post("/telegram/webhook", json={"update_id": 10})
            ).status_code == 403
            response = await client.post(
                "/telegram/webhook",
                headers={"X-Telegram-Bot-Api-Secret-Token": "secret"},
                json={"update_id": 10},
            )
            assert response.status_code == 200
            assert fake_bot.processed == 10
