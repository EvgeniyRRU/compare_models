"""Pytest fixtures and shared setup.

Required settings must be present *before* any ``app`` module is imported,
because :mod:`app.config` instantiates a module-level ``Settings`` (and
:mod:`app.db.session` builds the engine) at import time. Pytest imports this
conftest before collecting test modules, so ``setdefault`` here is sufficient.
"""

import os

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("DEV_CHAT_ID", "1")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://support:support@127.0.0.1:5433/support",
)
os.environ.setdefault("BOOTSTRAP_ADMIN_IDS", "123,456")
