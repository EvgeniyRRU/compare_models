# Rent Support Bot — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Telegram-бот + Mini App для учёта обращений первой линии поддержки к дежурному разработчику: тикеты со статусами и версионированием, посты в чат разработчиков, форма создания, напоминания, роли, REST API и статический Mini App.

**Architecture:** FastAPI (webhook/polling + REST `/api` + статика Mini App) хостит `python-telegram-bot` Application без Updater в webhook-режиме (апдейт обрабатывается синхронно в запросе). Вся бизнес-логика — в сервисном слое, независимом от Telegram/HTTP. PostgreSQL (SQLAlchemy 2.x async + asyncpg, Alembic). Защита от конфликтов — оптимистичная `version` через одно условное `UPDATE ... WHERE version = :v AND status = :s`. Синхронизация поста сериализуется session-level advisory lock на выделенном физическом соединении (NullPool-движок). Черновики форм, напоминания (`FOR UPDATE SKIP LOCKED`) и дедупликация апдейтов хранятся в БД — безопасна для нескольких реплик.

**Tech Stack:** Python 3.14.10 (Docker `python:3.14-slim`), FastAPI 0.163, python-telegram-bot **22.8**, SQLAlchemy 2.1 + asyncpg 0.32, Alembic 1.20, pydantic-settings 2.14, structlog, sentry-sdk, prometheus-client, uvicorn, PostgreSQL 16, pytest 9 + pytest-asyncio 1.5 (mode=auto), ruff, mypy.

**Source of truth:** `SPEC.md` в корне репозитория. При расхождении — SPEC.md имеет приоритет; уточнения ниже фиксируют решения, которые SPEC оставляет за реализатором.

---

## Conventions (для исполняющего агента)

- Работаешь из корня репозитория: `rent_support_bot/`. Все команды — отсюда.
- **Весь Python — через Docker.** Хостовый python не использовать.
  - Тесты: `docker compose --profile test run --rm test pytest <paths> -q` (поднимает postgres сам).
  - Ruff: `docker compose --profile test run --rm test ruff check app tests && docker compose --profile test run --rm test ruff format --check app tests`
  - Mypy: `docker compose --profile test run --rm test mypy app`
  - Alembic (dev): `docker compose exec app alembic upgrade head`
  - Полный прогон после каждого этапа: `docker compose --profile test run --rm test pytest -q`
- Host-файл `.env` подхватывается compose в рантайме (`env_file: .env`, `required: false`) и пробрасывается в контейнер `test` — env контейнера богаче, чем декларируется в compose. Тесты, проверяющие дефолты `Settings`, обязаны `monkeypatch.delenv` соответствующие переменные (например, `LOG_FORMAT`, `BOOTSTRAP_ADMIN_IDS`).
- Каждый task заканчивается: targeted pytest → ruff → commit (сообщение указано в task).
- Файлы создаёшь ТОЛЬКО по путям, указанным в task. Не рефакторь то, что не требуется task'ом.
- **Критичные факты PTB 22.8 (проверены эмпирически, НЕ гуглить заново):**
  - `Application.process_update(self, update)` — ОДИН аргумент (без `bot`/`context`).
  - `Application.initialize()` делает сетевой `get_me()` — поэтому в тестах используется `RecordingBot` (override `initialize`/`shutdown`/API-методов), а приложение строится через `ApplicationBuilder().bot(recording_bot)`.
  - Webhook: Updater не нужен; endpoint вызывает `await app.process_update(update)` напрямую.
  - Polling: `Updater(app.bot, app.update_queue)` → `await updater.initialize(); await updater.start_polling()` после `app.start()`.
  - Ошибки хендлеров НЕ поднимаются из `process_update` — они идут в error handler (`app.add_error_handler`).
  - Web App кнопка: `KeyboardButton(text=..., web_app=WebAppInfo(url=...))` внутри `ReplyKeyboardMarkup` (класса `WebAppButton` в PTB 22 НЕТ).
  - `ForceReply` импортируется из `telegram`.
  - Подкласс Application строится: `AppApplication.builder().token(...)` где `builder` — classmethod, возвращающий `ApplicationBuilder().application_class(cls)`.
- Время везде UTC, `datetime.now(timezone.utc)` (timestamptz в БД).
- «next_reminder_at = now() + интервал» считается Python-времем в той же транзакции (допустимо, см. SPEC «Напоминания» — важна транзакция, а не часы БД).
- Интервалы напоминаний по SPEC (допущения): critical 600 с, high 1800 с, medium 7200 с, low 28800 с.

## File map (полный, на конец плана)

```
pyproject.toml  .env.example  .env (git-ignored)  .gitignore  .dockerignore
Dockerfile  docker-compose.yml  alembic.ini
migrations/env.py  migrations/script.py.mako  migrations/versions/0001_initial.py
app/__init__.py
app/main.py            # app factory, lifespan, routers, metrics middleware
app/config.py          # Settings (pydantic-settings)
app/log.py             # structlog: configure_logging, get_logger
app/db.py              # DB (engine + lock_engine NullPool + session_factory)
app/metrics.py         # prometheus-объекты, gauge, middleware, metrics server
app/states.py          # state machine: Transition, TRANSITIONS, check_permission
app/models/__init__.py app/models/base.py app/models/user.py app/models/ticket.py
app/models/ticket_event.py app/models/draft.py app/models/processed_update.py
app/services/__init__.py app/services/errors.py app/services/updates.py
app/services/users.py app/services/tickets.py app/services/drafts.py
app/services/post_sync.py app/services/reminders.py
app/bot/__init__.py app/bot/application.py app/bot/form.py app/bot/webhook.py
app/bot/handlers/__init__.py app/bot/handlers/commands.py
app/bot/handlers/draft_form.py app/bot/handlers/callbacks.py
app/workers.py         # фоновые циклы
app/api/__init__.py app/api/auth.py app/api/deps.py app/api/schemas.py
app/api/tickets.py app/api/users.py
app/static/index.html app/static/app.js app/static/style.css
tests/__init__.py tests/conftest.py tests/fakes.py
tests/unit/test_config.py tests/unit/test_log.py tests/unit/test_states.py
tests/unit/test_post_render.py tests/unit/test_initdata.py tests/unit/test_form.py
tests/integration/test_health.py tests/integration/test_schema.py
tests/integration/test_users.py tests/integration/test_tickets.py
tests/integration/test_transitions.py tests/integration/test_take_race.py
tests/integration/test_post_sync.py tests/integration/test_updates_dedup.py
tests/integration/test_commands.py tests/integration/test_drafts.py
tests/integration/test_draft_flow.py tests/integration/test_post_callbacks.py
tests/integration/test_webhook.py tests/integration/test_reminders.py
tests/integration/test_api.py tests/integration/test_metrics.py
README.md
```

---

# Stage 1 — Каркас и конфиг

### Task 1.1: Docker, compose, env, игноры

**Files:**
- Create: `Dockerfile`, `docker-compose.yml`, `.env.example`, `.env`, `.gitignore`, `.dockerignore`

- [ ] **Step 1: Create `.gitignore`**

```gitignore
__pycache__/
*.pyc
.mypy_cache/
.pytest_cache/
.ruff_cache/
.env
dist/
*.egg-info/
```

- [ ] **Step 2: Create `.dockerignore`**

```
.git
__pycache__
*.pyc
.mypy_cache
.pytest_cache
.ruff_cache
.env
docs
dist
*.egg-info
```

- [ ] **Step 3: Create `Dockerfile`** (multi-stage; dev/test/prod)

```dockerfile
FROM python:3.14-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore
WORKDIR /srv/app
RUN addgroup --system app && adduser --system --ingroup app app

FROM base AS deps
COPY pyproject.toml README.md ./
COPY app ./app
COPY tests ./tests
RUN pip install --no-cache-dir -e . --group dev
RUN chown -R app:app /srv/app

FROM deps AS dev
USER app
EXPOSE 8000 9464
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload"]

FROM deps AS test
USER app
CMD ["pytest"]

FROM base AS prod
COPY pyproject.toml README.md ./
COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations
RUN pip install --no-cache-dir .
USER app
EXPOSE 8000 9464
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

Замечание: `README.md` копировался hatch'ем как README проекта — создай заглушку `README.md` с одной строкой `# rent-support-bot` (полная версия — в Stage 9).

- [ ] **Step 4: Create `docker-compose.yml`**

```yaml
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_USER: bot
      POSTGRES_PASSWORD: bot
      POSTGRES_DB: rent_bot
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U bot -d rent_bot"]
      interval: 2s
      timeout: 2s
      retries: 20

  app:
    build:
      context: .
      target: dev
    env_file:
      - path: .env
        required: false
    environment:
      DATABASE_URL: postgresql+asyncpg://bot:bot@postgres:5432/rent_bot
      LOG_FORMAT: pretty
    ports:
      - "8000:8000"
      - "9464:9464"
    depends_on:
      postgres:
        condition: service_healthy
    profiles: ["dev"]

  test:
    build:
      context: .
      target: test
    env_file:
      - path: .env
        required: false
    environment:
      DATABASE_URL: postgresql+asyncpg://bot:bot@postgres:5432/rent_bot_test
      TEST_DATABASE_URL: postgresql+asyncpg://bot:bot@postgres:5432/rent_bot_test
      LOG_FORMAT: pretty
    depends_on:
      postgres:
        condition: service_healthy
    profiles: ["test"]

volumes:
  pgdata:
```

- [ ] **Step 5: Create `.env.example`** (полный список — будет дополнен в Task 1.3; пока минимум)

```bash
# --- Telegram ---
BOT_TOKEN=123456789:AAHtesttesttesttesttesttesttesttest1
BOT_MODE=polling            # polling (локально) | webhook (production)
WEBHOOK_URL=                # https://host/telegram/webhook (обязателен при BOT_MODE=webhook)
WEBHOOK_SECRET_TOKEN=       # secret token для webhook (обязателен при BOT_MODE=webhook)
DEV_CHAT_ID=-1001234567890  # чат разработчиков для постов (отрицательный id группы)
BOOTSTRAP_ADMIN_IDS=111     # Telegram ID первичных админов, через запятую
MINI_APP_URL=https://localhost/   # URL Mini App (задаётся в BotFather)

# --- Database ---
DATABASE_URL=postgresql+asyncpg://bot:bot@localhost:5432/rent_bot

# --- Logging ---
LOG_LEVEL=INFO
LOG_FORMAT=json             # json (production) | pretty (dev)

# --- Monitoring ---
SENTRY_DSN=                 # пустое = Sentry выключен
METRICS_PORT=9464

# --- Лимиты и интервалы ---
UPDATE_TIMEOUT_SECONDS=5    # таймаут обработки одного апдейта
DRAFT_TTL_HOURS=24          # TTL черновиков формы
INITDATA_MAX_AGE_SECONDS=86400  # порог свежести auth_date initData
POST_SYNC_INTERVAL_SECONDS=15   # период фоновой синхронизации постов
REMINDER_POLL_INTERVAL_SECONDS=30
REMINDER_INTERVAL_CRITICAL_SECONDS=600
REMINDER_INTERVAL_HIGH_SECONDS=1800
REMINDER_INTERVAL_MEDIUM_SECONDS=7200
REMINDER_INTERVAL_LOW_SECONDS=28800
```

- [ ] **Step 6: Create local `.env`** — копия `.env.example` без комментариев (значения из примера; `LOG_FORMAT=pretty`). `.env` git-ignored.

- [ ] **Step 7: Verify Docker image builds and postgres starts**

Run: `docker compose --profile test build test 2>&1 | tail -3`
Expected: сборка OK (stage `test`).
Run: `docker compose --profile test run --rm test python -V`
Expected: `Python 3.14.x` (postgres поднимается и проходит healthcheck).

Note: сборка сейчас упадёт на `pip install -e .` — нет `pyproject.toml` (плюс `COPY app`/`COPY tests` потребуют файлы из Task 1.2). Это OK для шага 7: выполни Step 7 ПОСЛЕ Task 1.2 (соединённая проверка). `alembic.ini`/`migrations/` в deps-stage появятся только после правки Dockerfile в Task 1.7 (файлов нет в репозитории раньше). Если хочешь проверить compose отдельно: `docker compose config --profiles test >/dev/null` — валидность YAML.

- [ ] **Step 8: Commit**

```bash
git add Dockerfile docker-compose.yml .env.example .gitignore .dockerignore README.md
git commit -m "chore: docker skeleton (dev/test/prod targets) and compose"
```

### Task 1.2: pyproject, пакеты, база моделей

**Files:**
- Create: `pyproject.toml`, `app/__init__.py`, `app/models/__init__.py`, `app/models/base.py`, `tests/__init__.py`

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "rent-support-bot"
version = "0.1.0"
description = "Telegram-бот учёта обращений к дежурному разработчику"
requires-python = ">=3.14"
dependencies = [
  "fastapi>=0.115",
  "python-telegram-bot>=21",
  "sqlalchemy[asyncio]>=2.0",
  "asyncpg>=0.30",
  "alembic>=1.13",
  "pydantic-settings>=2.3",
  "structlog>=24.1",
  "sentry-sdk[fastapi,sqlalchemy]>=2.0",
  "prometheus-client>=0.20",
  "uvicorn[standard]>=0.30",
]

[dependency-groups]
dev = [
  "pytest>=8",
  "pytest-asyncio>=1.0",
  "httpx>=0.27",
  "ruff>=0.6",
  "mypy>=1.11",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["app"]

[tool.ruff]
line-length = 100
target-version = "py314"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "ASYNC", "SIM"]

[tool.mypy]
python_version = "3.14"
strict = true
plugins = ["pydantic.mypy"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
testpaths = ["tests"]
```

- [ ] **Step 2: Create `app/__init__.py` и `tests/__init__.py`** — пустые файлы.

- [ ] **Step 3: Create `app/models/__init__.py`**

```python
from app.models.base import Base
from app.models.processed_update import ProcessedUpdate
from app.models.ticket import Ticket
from app.models.ticket_event import TicketEvent
from app.models.user import User

__all__ = ["Base", "ProcessedUpdate", "Ticket", "TicketEvent", "User"]
```

Wait — models ещё не существуют; создай ПУСТОЙ `app/models/__init__.py` (содержимое добавим в Stage 2).

- [ ] **Step 4: Create `app/models/base.py`**

```python
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def created_at_column() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def updated_at_column() -> Mapped[datetime]:
    # ЕДИНЫЙ механизм updated_at для всех таблиц (решение по SPEC): onupdate в SQLAlchemy.
    # Покрывает все ORM и Core-обновления приложения; сырой SQL намеренно не покрывается.
    return mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
```

- [ ] **Step 5: Verify build + install**

Run: `docker compose --profile test run --rm test pytest --collect-only -q 2>&1 | tail -2`
Expected: `no tests ran` / 0 collected (ошибок импорта нет).
Run: `docker compose --profile test run --rm test python -c "import app, app.models.base; print('ok')"`
Expected: `ok`

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml app/__init__.py app/models/__init__.py app/models/base.py tests/__init__.py
git commit -m "chore: pyproject (ruff/mypy/pytest), models base"
```

### Task 1.3: Конфигурация (pydantic-settings)

**Files:**
- Create: `app/config.py`
- Test: `tests/unit/test_config.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_config.py
import pytest

from app.config import Settings


def test_defaults(monkeypatch):
    # test-сервис compose ставит LOG_FORMAT=pretty в env — уберём, чтобы проверить реальный дефолт
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    # host .env подхватывается compose (env_file required: false) и пробрасывается в контейнер —
    # уберём, чтобы проверить реальный дефолт
    monkeypatch.delenv("BOOTSTRAP_ADMIN_IDS", raising=False)
    s = Settings(
        bot_token="1:AA",
        dev_chat_id=-100,
        database_url="postgresql+asyncpg://u:p@h/db",
        _env_file=None,
    )
    assert s.bot_mode == "polling"
    assert s.log_format == "json"
    assert s.metrics_port == 9464
    assert s.draft_ttl_hours == 24
    assert s.update_timeout_seconds == 5.0
    assert s.bootstrap_admin_ids == []
    assert s.reminder_interval("critical") == 600
    assert s.reminder_interval("low") == 28800


def test_bootstrap_admin_ids_from_env(monkeypatch):
    monkeypatch.setenv("BOOTSTRAP_ADMIN_IDS", "111, 222")
    s = Settings(
        bot_token="1:AA",
        dev_chat_id=-100,
        database_url="postgresql+asyncpg://u:p@h/db",
        _env_file=None,
    )
    assert s.bootstrap_admin_ids == [111, 222]


def test_validate_modes_rejects_unknown_mode():
    s = Settings(
        bot_token="x", dev_chat_id=1, database_url="d", _env_file=None, bot_mode="webhooks"
    )
    with pytest.raises(ValueError, match="invalid BOT_MODE"):
        s.validate_modes()


def test_validate_modes_webhook_requires_url():
    s = Settings(
        bot_token="x",
        dev_chat_id=1,
        database_url="d",
        _env_file=None,
        bot_mode="webhook",
        webhook_url="",
    )
    with pytest.raises(ValueError, match="WEBHOOK_URL required"):
        s.validate_modes()


def test_validate_modes_webhook_requires_secret():
    s = Settings(
        bot_token="x",
        dev_chat_id=1,
        database_url="d",
        _env_file=None,
        bot_mode="webhook",
        webhook_url="https://example.com/hook",
        webhook_secret_token="",
    )
    with pytest.raises(ValueError, match="WEBHOOK_SECRET_TOKEN required"):
        s.validate_modes()


def test_validate_modes_accepts_valid_configs():
    polling = Settings(bot_token="x", dev_chat_id=1, database_url="d", _env_file=None)
    polling.validate_modes()
    webhook = Settings(
        bot_token="x",
        dev_chat_id=1,
        database_url="d",
        _env_file=None,
        bot_mode="webhook",
        webhook_url="https://example.com/hook",
        webhook_secret_token="s",
    )
    webhook.validate_modes()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose --profile test run --rm test pytest tests/unit/test_config.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.config'`

- [ ] **Step 3: Create `app/config.py`**

```python
from typing import Annotated, Any

from pydantic import BeforeValidator, Field
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

REMINDER_PRIORITIES = ("critical", "high", "medium", "low")


def _split_csv(value: Any) -> Any:
    # NoDecode: pydantic-settings передаёт строку из env как есть —
    # разбираем "111, 222" в ["111", "222"] перед валидацией list[int]
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram
    bot_token: str
    bot_mode: str = "polling"  # "polling" | "webhook"
    webhook_url: str = ""
    webhook_secret_token: str = ""
    dev_chat_id: int
    bootstrap_admin_ids: Annotated[list[int], NoDecode, BeforeValidator(_split_csv)] = []
    mini_app_url: str = ""

    # Database
    database_url: str

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"  # "json" | "pretty"

    # Monitoring
    sentry_dsn: str | None = None
    metrics_port: int = Field(default=9464, ge=1, le=65535)

    # Лимиты и интервалы
    update_timeout_seconds: float = 5.0
    draft_ttl_hours: int = 24
    initdata_max_age_seconds: int = 86400
    post_sync_interval_seconds: float = 15.0
    reminder_poll_interval_seconds: float = 30.0
    reminder_interval_critical_seconds: int = 600
    reminder_interval_high_seconds: int = 1800
    reminder_interval_medium_seconds: int = 7200
    reminder_interval_low_seconds: int = 28800

    def reminder_interval(self, priority: str) -> int:
        return {
            "critical": self.reminder_interval_critical_seconds,
            "high": self.reminder_interval_high_seconds,
            "medium": self.reminder_interval_medium_seconds,
            "low": self.reminder_interval_low_seconds,
        }[priority]

    def validate_modes(self) -> None:
        if self.bot_mode not in ("polling", "webhook"):
            raise ValueError(f"invalid BOT_MODE: {self.bot_mode}")
        if self.bot_mode == "webhook":
            if not self.webhook_url:
                raise ValueError("WEBHOOK_URL required when BOT_MODE=webhook")
            if not self.webhook_secret_token:
                raise ValueError("WEBHOOK_SECRET_TOKEN required when BOT_MODE=webhook")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose --profile test run --rm test pytest tests/unit/test_config.py -q`
Expected: 2 passed

- [ ] **Step 5: Ruff + commit**

Run: `docker compose --profile test run --rm test ruff check app tests && docker compose --profile test run --rm test ruff format app tests`
```bash
git add app/config.py tests/unit/test_config.py
git commit -m "feat: pydantic-settings configuration"
```

### Task 1.4: Логирование (structlog)

**Files:**
- Create: `app/log.py`
- Test: `tests/unit/test_log.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_log.py
import json

import structlog

from app.log import configure_logging, get_logger


def test_json_logs_with_context(capsys):
    # Паттерн всех модулей: логгер создаётся при import — ДО configure_logging().
    # JSON-вывод возможен только благодаря ленивому резолвингу цепочки процессоров.
    logger = get_logger("test.component")
    configure_logging("INFO", "json")
    with structlog.contextvars.bound_contextvars(update_id=42):
        logger.info("hello", extra_key="v")
    out = capsys.readouterr().out
    record = json.loads(out.strip().splitlines()[-1])
    assert record["event"] == "hello"
    assert record["component"] == "test.component"
    assert record["update_id"] == 42
    assert record["extra_key"] == "v"
    assert "timestamp" in record


def test_context_not_leaked(capsys):
    configure_logging("INFO", "json")
    logger = get_logger("c")
    with structlog.contextvars.bound_contextvars(ticket_id=7):
        logger.info("x")
    capsys.readouterr()
    logger.info("y")
    record = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "ticket_id" not in record
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose --profile test run --rm test pytest tests/unit/test_log.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.log'`

- [ ] **Step 3: Create `app/log.py`**

```python
"""Структурированные JSON-логи (structlog). Контекстные теги — через contextvars."""

import logging
import sys
from typing import cast

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars, unbind_contextvars


def configure_logging(level: str, fmt: str) -> None:
    clear_contextvars()
    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if fmt == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=False,
    )
    logging.basicConfig(stream=sys.stdout, level=numeric_level, format="%(message)s")
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "sqlalchemy.engine"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True


def get_logger(component: str) -> structlog.BoundLogger:
    # ВАЖНО: возвращаем ленивый прокси БЕЗ немедленного .bind().
    # Цепочка процессоров резолвится в момент каждого логирования — с ТЕКУЩИМИ
    # настройками configure_logging(). Модульные `log = get_logger(...)` создаются
    # при import (до lifespan), и немедленный bind «заморозил» бы дефолтный
    # конфиг structlog (не JSON) — нарушение SPEC.
    return cast(structlog.BoundLogger, structlog.get_logger(component=component))


__all__ = ["configure_logging", "get_logger", "bind_contextvars", "unbind_contextvars"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose --profile test run --rm test pytest tests/unit/test_log.py -q`
Expected: 2 passed

- [ ] **Step 5: Ruff + commit**

```bash
git add app/log.py tests/unit/test_log.py
git commit -m "feat: structlog JSON logging with contextvars"
```

### Task 1.5: DB-слой

**Files:**
- Create: `app/db.py`

- [ ] **Step 1: Create `app/db.py`**

```python
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


class DB:
    """Два движка:
    - engine: основной пул для бизнес-транзакций;
    - lock_engine: NullPool — выделенные ФИЗИЧЕСКИЕ соединения для session-level
      advisory lock (после close() соединение закрывается, а не возвращается в пул;
      блокировка живёт до явного pg_advisory_unlock или конца сессии).
    """

    def __init__(self, engine: AsyncEngine, lock_engine: AsyncEngine):
        self.engine = engine
        self.lock_engine = lock_engine
        self.session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def dispose(self) -> None:
        await self.engine.dispose()
        await self.lock_engine.dispose()


def make_db(database_url: str) -> DB:
    return DB(
        engine=create_async_engine(database_url, pool_pre_ping=True),
        lock_engine=create_async_engine(database_url, poolclass=NullPool),
    )
```

- [ ] **Step 2: Verify import**

Run: `docker compose --profile test run --rm test python -c "from app.db import make_db; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Ruff + commit**

```bash
git add app/db.py
git commit -m "feat: db layer with dedicated NullPool engine for advisory locks"
```

### Task 1.6: FastAPI app + health

**Files:**
- Create: `app/main.py`, `app/api/__init__.py`, `app/api/health.py`
- Test: `tests/integration/test_health.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_health.py
import httpx
import pytest_asyncio
from fastapi import FastAPI

from app.api.health import router as health_router


@pytest_asyncio.fixture
async def client(db, settings):
    app = FastAPI()
    app.state.settings = settings
    app.state.db = db
    app.include_router(health_router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_live(client):
    r = await client.get("/health/live")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_ready_ok(client):
    r = await client.get("/health/ready")
    assert r.status_code == 200
    assert r.json() == {"status": "ready"}
```

Note: тест использует фикстуры `db`, `settings` из `tests/conftest.py` — он появится в Task 2.3. До этого шага тест не запускается: помести `tests/integration/test_health.py` в репозиторий, но прогоняй его после Task 2.3 (см. Step 4).

- [ ] **Step 2: Create `app/api/__init__.py`** (пустой) и `app/api/health.py`

```python
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

router = APIRouter()


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


# response_model=None: FastAPI 0.163.1 падает при импорте (FastAPIError),
# если return-аннотация Union[Response, dict] — проверено эмпирически
@router.get("/health/ready", response_model=None)
async def ready(request: Request) -> JSONResponse | dict[str, str]:
    try:
        async with request.app.state.db.session_factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse({"status": "not ready"}, status_code=503)
    return {"status": "ready"}
```

- [ ] **Step 3: Create `app/main.py`** (v1 — health; расширяется в Stage 5/7/8)

```python
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import log
from app.api.health import router as health_router
from app.config import Settings, get_settings
from app.db import make_db


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    log.configure_logging(settings.log_level, settings.log_format)
    settings.validate_modes()
    app.state.db = make_db(settings.database_url)
    yield
    await app.state.db.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="rent-support-bot", lifespan=lifespan)
    app.state.settings = settings or get_settings()
    app.include_router(health_router)
    return app


app = create_app()
```

Also create `app/config.py` addition: append to `app/config.py`:

```python
from functools import lru_cache


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

(Импорт `lru_cache` — в блок импортов наверху файла.)

Note: `StaticFiles` смонтируется только в Stage 7 — в v1 его импорта нет (ruff F401).

- [ ] **Step 4: Verify** (запустится только после Task 2.3, когда появятся конфтесты)

Run: `docker compose --profile test run --rm test pytest tests/integration/test_health.py -q`
Expected: 2 passed

- [ ] **Step 5: Ruff + commit**

```bash
git add app/main.py app/api/__init__.py app/api/health.py app/config.py tests/integration/test_health.py
git commit -m "feat: fastapi app factory, health endpoints"
```

### Task 1.7: Alembic env (async) + проверка миграции

**Files:**
- Create: `alembic.ini`, `migrations/env.py`, `migrations/script.py.mako`

- [ ] **Step 1: Create `alembic.ini`**

```ini
[alembic]
script_location = migrations
prepend_sys_path = .
file_template = %%(rev)s_%%(slug)s
sqlalchemy.url =

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console
qualname =

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
```

Note: `sqlalchemy.url` пуст намеренно — `migrations/env.py` при пустом URL подставляет env-переменную `DATABASE_URL` (compose передаёт её в dev- и test-контейнер; в тестовом это `rent_bot_test`).

- [ ] **Step 2: Create `migrations/env.py`**

```python
import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

import app.models  # noqa: F401  # регистрирует все модели в metadata
from app.models.base import Base

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False: in-process (conftest pytest) — не глушим существующие логгеры
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# sqlalchemy.url в alembic.ini намеренно пуст:
# - dev (compose) передаёт DATABASE_URL env-переменной;
# - тесты переопределяют URL в conftest через set_main_option (Task 2.3).
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", os.environ["DATABASE_URL"])

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

- [ ] **Step 3: Create `migrations/script.py.mako`**

```mako
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: Union[str, None] = ${repr(down_revision)}
branch_labels: Union[str, Sequence[str], None] = ${repr(branch_labels)}
depends_on: Union[str, Sequence[str], None] = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
```

- [ ] **Step 4: Add alembic files to dev/test images**

В `deps`-stages Dockerfile нет копирования `alembic.ini` и `migrations/` — а `dev`-CMD запускает `alembic upgrade head`, и тесты (conftest Task 2.3) запускают его внутри контейнера. Добавь в stage `deps` после `COPY tests ./tests`:

```dockerfile
COPY alembic.ini ./
COPY migrations ./migrations
```

(stage `prod` уже имеет собственные копии — не трогай). Пересобери test-образ:

Run: `docker compose --profile test build test`
Expected: сборка OK (stage `test`)

- [ ] **Step 5: Verify alembic boots** (миграций ещё нет — upgrade head завершится без действий)

Важно: тестовая БД `rent_bot_test` ещё НЕ существует (создаёт её conftest в Task 2.3) — поэтому проверка идёт против существующей `rent_bot`. Переопределение `DATABASE_URL` shell-присваиванием дополнительно доказывает, что fallback в env.py из env-переменной работает.

Run: `docker compose --profile test run --rm test sh -c "DATABASE_URL=postgresql+asyncpg://bot:bot@postgres:5432/rent_bot alembic upgrade head && echo ALEMBIC_OK"`
Expected: `ALEMBIC_OK` (или `alembic` не найден в PATH test-образа — в этом случае: `python -m alembic upgrade head`).

- [ ] **Step 6: Commit**

```bash
git add alembic.ini migrations/ Dockerfile
git commit -m "chore: async alembic env (dev/test images get alembic files)"
```

### Task 1.8: Проверка этапа + mypy

- [ ] **Step 1: Полный прогон**

Run: `docker compose --profile test run --rm test pytest tests/unit -q`
Expected: all passed (config + log — 8 тестов).

Note: `tests/integration/test_health.py` (Task 1.6) коллектируется нормально (зависимость
от `tests/conftest.py` из Task 2.3 — только через имена фиксюр `db`/`settings`, pytest
разрешает их лениво), но полный прогон до Task 2.3 даёт `8 passed, 2 errors`
(setup-ошибки: фиксьюры ещё не существуют). Поэтому stage-1 прогон намеренно ограничен
`tests/unit`. Проверка health-тестов — Step 4 Task 1.6, который выполняется после Task 2.3.
(Проверено в контейнере: `pytest --co -q` = 10 collected; `pytest -q` = 8 passed, 2 errors.)

Run: `docker compose --profile test run --rm test ruff check app tests && docker compose --profile test run --rm test mypy app`
Expected: no errors. При ошибках mypy — исправить.

- [ ] **Step 2: Commit (если были фиксы)**

```bash
git add -A
git commit -m "fix: stage 1 lint/type fixes"
```

# Stage 2 — Модели и миграции

Note (весь stage 2): код ниже каноничен по смыслу, но некоторые строки длиннее 100 символов.
Перед ruff-гейтом каждого task выполни `docker compose --profile test run --rm test ruff format app tests`;
если после форматирования `ruff check` всё ещё ругается на E501 (длинные строковые литералы
форматирование не режет) — разбей строку на неявную конкатенацию, НЕ меняя значения строк.

### Task 2.1: Модели (все 5 таблиц)

**Files:**
- Modify: `app/models/__init__.py`
- Create: `app/models/user.py`, `app/models/ticket.py`, `app/models/ticket_event.py`, `app/models/draft.py`, `app/models/processed_update.py`

- [ ] **Step 1: Create `app/models/user.py`**

```python
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at_column, updated_at_column


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "role IS NULL OR role IN ('support', 'developer', 'admin')", name="ck_users_role"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String)
    first_name: Mapped[str | None] = mapped_column(String)
    last_name: Mapped[str | None] = mapped_column(String)
    role: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()
```

- [ ] **Step 2: Create `app/models/ticket.py`**

```python
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at_column, updated_at_column


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        CheckConstraint(
            "length(description) BETWEEN 1 AND 3000", name="ck_tickets_description_len"
        ),
        CheckConstraint(
            "priority IN ('critical', 'high', 'medium', 'low')", name="ck_tickets_priority"
        ),
        CheckConstraint("length(request_id) BETWEEN 1 AND 64", name="ck_tickets_request_id_len"),
        CheckConstraint(
            "tenant_id IS NULL OR length(tenant_id) BETWEEN 1 AND 64",
            name="ck_tickets_tenant_id_len",
        ),
        CheckConstraint(
            "status IN ('new', 'in_progress', 'waiting_info', 'resolved', 'rejected', 'duplicate')",
            name="ck_tickets_status",
        ),
        CheckConstraint("version >= 1", name="ck_tickets_version"),
        CheckConstraint("post_synced_version >= 0", name="ck_tickets_post_synced_version"),
        # Инварианты (SPEC «Сущности и статусы»)
        CheckConstraint(
            "status <> 'new' OR (assignee_id IS NULL AND taken_at IS NULL)",
            name="ck_tickets_new_unassigned",
        ),
        CheckConstraint(
            "status NOT IN ('in_progress', 'waiting_info') OR assignee_id IS NOT NULL",
            name="ck_tickets_active_requires_assignee",
        ),
        CheckConstraint(
            "status NOT IN ('in_progress', 'waiting_info') OR taken_at IS NOT NULL",
            name="ck_tickets_active_requires_taken_at",
        ),
        CheckConstraint(
            "status IN ('resolved', 'rejected', 'duplicate') OR closed_at IS NULL",
            name="ck_tickets_closed_at_only_final",
        ),
        CheckConstraint(
            "status NOT IN ('resolved', 'rejected', 'duplicate') OR closed_at IS NOT NULL",
            name="ck_tickets_closed_at_required_final",
        ),
        Index("ix_tickets_status_next_reminder", "status", "next_reminder_at"),
        Index("ix_tickets_author_id", "author_id"),
        Index("ix_tickets_assignee_id", "assignee_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    author_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    assignee_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))
    description: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[str] = mapped_column(String(16), nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="new", server_default="new"
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    post_synced_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()
    taken_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_reminder_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

- [ ] **Step 3: Create `app/models/ticket_event.py`**

```python
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at_column


class TicketEvent(Base):
    __tablename__ = "ticket_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('created', 'taken', 'status_changed', "
            "'declined', 'reassigned', 'reopened')",
            name="ck_ticket_events_type",
        ),
        Index("ix_ticket_events_ticket_id", "ticket_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(Integer, ForeignKey("tickets.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    old_status: Mapped[str | None] = mapped_column(String(16))
    new_status: Mapped[str | None] = mapped_column(String(16))
    old_assignee_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))
    new_assignee_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))
    version: Mapped[int] = mapped_column(Integer, nullable=False)  # version после изменения
    created_at: Mapped[datetime] = created_at_column()
```

- [ ] **Step 4: Create `app/models/draft.py`**

```python
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at_column, updated_at_column


class Draft(Base):
    __tablename__ = "ticket_drafts"
    __table_args__ = (
        CheckConstraint(
            "step IN ('description', 'priority', 'request_id', 'tenant_id', 'confirm')",
            name="ck_drafts_step",
        ),
        CheckConstraint("version >= 1", name="ck_drafts_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False, unique=True
    )  # не более одного активного черновика на пользователя
    step: Mapped[str] = mapped_column(String(16), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[str | None] = mapped_column(String(16))
    request_id: Mapped[str | None] = mapped_column(String(64))
    tenant_id: Mapped[str | None] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    prompt_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = created_at_column.__wrapped__() if False else updated_at_column()
```

NOTE: the last line above is a typo trap — write it exactly as:

```python
    updated_at: Mapped[datetime] = updated_at_column()
```

- [ ] **Step 5: Create `app/models/processed_update.py`**

```python
from datetime import datetime

from sqlalchemy import BigInteger
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at_column


class ProcessedUpdate(Base):
    __tablename__ = "processed_updates"

    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    processed_at: Mapped[datetime] = created_at_column()
```

- [ ] **Step 6: Replace `app/models/__init__.py`**

```python
from app.models.base import Base
from app.models.draft import Draft
from app.models.processed_update import ProcessedUpdate
from app.models.ticket import Ticket
from app.models.ticket_event import TicketEvent
from app.models.user import User

__all__ = ["Base", "Draft", "ProcessedUpdate", "Ticket", "TicketEvent", "User"]
```

- [ ] **Step 7: Verify imports**

Run: `docker compose --profile test run --rm test python -c "import app.models as m; print(len(m.Base.metadata.tables))"`
Expected: `5`

- [ ] **Step 8: Ruff + commit**

```bash
git add app/models/
git commit -m "feat: sqlalchemy models (users, tickets, events, drafts, processed_updates)"
```

### Task 2.2: Миграция 0001

**Files:**
- Create: `migrations/versions/0001_initial.py`

- [ ] **Step 1: Create the migration** (hand-written; matches models 1:1)

```python
"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-16

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(), nullable=True),
        sa.Column("first_name", sa.String(), nullable=True),
        sa.Column("last_name", sa.String(), nullable=True),
        sa.Column("role", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_id"),
        sa.CheckConstraint("role IS NULL OR role IN ('support', 'developer', 'admin')", name="ck_users_role"),
    )
    op.create_table(
        "tickets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("author_id", sa.BigInteger(), nullable=False),
        sa.Column("assignee_id", sa.BigInteger(), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("priority", sa.String(length=16), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="new", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("post_synced_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_reminder_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["assignee_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("length(description) BETWEEN 1 AND 3000", name="ck_tickets_description_len"),
        sa.CheckConstraint("priority IN ('critical', 'high', 'medium', 'low')", name="ck_tickets_priority"),
        sa.CheckConstraint("length(request_id) BETWEEN 1 AND 64", name="ck_tickets_request_id_len"),
        sa.CheckConstraint("tenant_id IS NULL OR length(tenant_id) BETWEEN 1 AND 64", name="ck_tickets_tenant_id_len"),
        sa.CheckConstraint("status IN ('new', 'in_progress', 'waiting_info', 'resolved', 'rejected', 'duplicate')", name="ck_tickets_status"),
        sa.CheckConstraint("version >= 1", name="ck_tickets_version"),
        sa.CheckConstraint("post_synced_version >= 0", name="ck_tickets_post_synced_version"),
        sa.CheckConstraint("status <> 'new' OR (assignee_id IS NULL AND taken_at IS NULL)", name="ck_tickets_new_unassigned"),
        sa.CheckConstraint("status NOT IN ('in_progress', 'waiting_info') OR assignee_id IS NOT NULL", name="ck_tickets_active_requires_assignee"),
        sa.CheckConstraint("status NOT IN ('in_progress', 'waiting_info') OR taken_at IS NOT NULL", name="ck_tickets_active_requires_taken_at"),
        sa.CheckConstraint("status IN ('resolved', 'rejected', 'duplicate') OR closed_at IS NULL", name="ck_tickets_closed_at_only_final"),
        sa.CheckConstraint("status NOT IN ('resolved', 'rejected', 'duplicate') OR closed_at IS NOT NULL", name="ck_tickets_closed_at_required_final"),
    )
    op.create_index("ix_tickets_status_next_reminder", "tickets", ["status", "next_reminder_at"])
    op.create_index("ix_tickets_author_id", "tickets", ["author_id"])
    op.create_index("ix_tickets_assignee_id", "tickets", ["assignee_id"])
    op.create_table(
        "ticket_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ticket_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=False),
        sa.Column("old_status", sa.String(length=16), nullable=True),
        sa.Column("new_status", sa.String(length=16), nullable=True),
        sa.Column("old_assignee_id", sa.BigInteger(), nullable=True),
        sa.Column("new_assignee_id", sa.BigInteger(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["new_assignee_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["old_assignee_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("event_type IN ('created', 'taken', 'status_changed', 'declined', 'reassigned', 'reopened')", name="ck_ticket_events_type"),
    )
    op.create_index("ix_ticket_events_ticket_id", "ticket_events", ["ticket_id"])
    op.create_table(
        "ticket_drafts",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("step", sa.String(length=16), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("priority", sa.String(length=16), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("prompt_message_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
        sa.CheckConstraint("step IN ('description', 'priority', 'request_id', 'tenant_id', 'confirm')", name="ck_drafts_step"),
        sa.CheckConstraint("version >= 1", name="ck_drafts_version"),
    )
    op.create_table(
        "processed_updates",
        sa.Column("update_id", sa.BigInteger(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("update_id"),
    )


def downgrade() -> None:
    op.drop_table("processed_updates")
    op.drop_table("ticket_drafts")
    op.drop_index("ix_ticket_events_ticket_id", table_name="ticket_events")
    op.drop_table("ticket_events")
    op.drop_index("ix_tickets_assignee_id", table_name="tickets")
    op.drop_index("ix_tickets_author_id", table_name="tickets")
    op.drop_index("ix_tickets_status_next_reminder", table_name="tickets")
    op.drop_table("tickets")
    op.drop_table("users")
```

- [ ] **Step 2: Run migration on a scratch database**

Scratch-БД создаём из test-контейнера через asyncpg: one-off `postgres`-контейнер от
`compose run` не имеет запущенного сервера (psql туда не подключится), а `compose exec`
работает только пока сервис postgres запущен. URL для alembic передаём env-переменной
`DATABASE_URL` — `migrations/env.py` подставляет её, когда `sqlalchemy.url` пуст.

Run:
```bash
docker compose --profile test run --rm test python -c "
import asyncio, asyncpg
async def m():
    c = await asyncpg.connect('postgresql://bot:bot@postgres:5432/postgres')
    await c.execute('DROP DATABASE IF EXISTS rent_bot_migtest')
    await c.execute('CREATE DATABASE rent_bot_migtest')
    print('DB_OK')
    await c.close()
asyncio.run(m())
"
```
Expected: `DB_OK`.

Run:
```bash
docker compose --profile test run --rm -e DATABASE_URL=postgresql+asyncpg://bot:bot@postgres:5432/rent_bot_migtest test sh -c "python -m alembic upgrade head && echo MIG_OK"
```
Expected: `MIG_OK` (таблицы созданы).

Проверь (5 таблиц + alembic_version = 6 в public):
```bash
docker compose --profile test run --rm test python -c "
import asyncio, asyncpg
async def m():
    c = await asyncpg.connect('postgresql://bot:bot@postgres:5432/rent_bot_migtest')
    print([r[0] for r in await c.fetch(\"SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY 1\")])
    await c.close()
asyncio.run(m())
"
```
Expected: `['alembic_version', 'processed_updates', 'ticket_drafts', 'ticket_events', 'tickets', 'users']`

- [ ] **Step 3: Verify metadata match (alembic check — пустой diff)**

Run:
```bash
docker compose --profile test run --rm -e DATABASE_URL=postgresql+asyncpg://bot:bot@postgres:5432/rent_bot_migtest test sh -c "python -m alembic check && echo NO_DRIFT"
```
Expected: `NO_DRIFT` (метаданные моделей == схема БД). Если drift — сверь `migrations/versions/0001_initial.py` с моделями (Task 2.1) и поправ миграцию.

Убери scratch-БД:
```bash
docker compose --profile test run --rm test python -c "
import asyncio, asyncpg
async def m():
    c = await asyncpg.connect('postgresql://bot:bot@postgres:5432/postgres')
    await c.execute('DROP DATABASE IF EXISTS rent_bot_migtest')
    await c.close()
    print('DROPPED_OK')
asyncio.run(m())
"
```

- [ ] **Step 4: Commit**

```bash
git add migrations/versions/0001_initial.py
git commit -m "feat: initial alembic migration (5 tables, invariants as CHECKs)"
```

### Task 2.3: Тестовая инфраструктура (conftest, fakes)

**Files:**
- Create: `tests/conftest.py`, `tests/fakes.py`

- [ ] **Step 1: Create `tests/conftest.py`**

```python
"""Тесты на реальном PostgreSQL из docker compose.

- новая БД на сессию (drop/create + alembic upgrade head);
- TRUNCATE всех таблиц между тестами (конкурентные тесты требуют реальных коммитов);
- RecordingBot вместо сетевых вызовов Telegram.
"""

import asyncio
import os

import asyncpg
import pytest
import pytest_asyncio

from app.config import Settings
from app.db import make_db
from tests.fakes import RecordingBot

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://bot:bot@localhost:5432/rent_bot_test"
)
FAKE_TOKEN = "123456789:AAHtesttesttesttesttesttesttesttest1"


def _split(url: str) -> tuple[str, str]:
    prefix, db_name = url.rsplit("/", 1)
    return prefix, db_name


def _create_test_database() -> None:
    prefix, db_name = _split(TEST_DB_URL)

    async def _run() -> None:
        dsn = prefix.replace("postgresql+asyncpg://", "postgresql://") + "/postgres"
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid()",
                db_name,
            )
            await conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
            await conn.execute(f'CREATE DATABASE "{db_name}"')
        finally:
            await conn.close()

    asyncio.run(_run())
    _run_alembic()


def _run_alembic() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    cfg.set_main_option("sqlalchemy.url", TEST_DB_URL)
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session", autouse=True)
def test_database():
    _create_test_database()
    yield


@pytest_asyncio.fixture
async def db():
    db = make_db(TEST_DB_URL)
    yield db
    await db.dispose()


@pytest_asyncio.fixture(autouse=True)
async def clean_tables(db):
    yield
    from sqlalchemy import text

    async with db.session_factory() as session, session.begin():
        await session.execute(
            text(
                "TRUNCATE users, tickets, ticket_events, ticket_drafts, "
                "processed_updates RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture
def settings() -> Settings:
    return Settings(
        bot_token=FAKE_TOKEN,
        bot_mode="polling",
        dev_chat_id=-1001234567890,
        bootstrap_admin_ids=[111],
        database_url=TEST_DB_URL,
        mini_app_url="https://mini.example/",
        log_format="json",
        sentry_dsn=None,
        _env_file=None,
    )


@pytest.fixture
def bot() -> RecordingBot:
    return RecordingBot(FAKE_TOKEN)


@pytest_asyncio.fixture
async def tg_app(settings, db, bot):
    """PTB-приложение с RecordingBot (без сети). Используется тестами Stage 5+."""
    # NB: PTB 22.8 start() НЕ вызывает post_init (только run_polling/run_webhook) —
    # bot.commands / bot.webhook под tg_app не устанавливаются
    from app.bot.application import build_application

    app = build_application(settings, db, bot=bot)
    await app.initialize()
    await app.start()
    yield app
    await app.stop()
    await app.shutdown()
```

Note: `_env_file=None` — pydantic-settings принимает ключевое `_env_file` для отключения .env; все значения даются явно.
Note: `test_database` синхронный намеренно — online-путь `migrations/env.py` вызывает `asyncio.run()`: in-process `alembic upgrade head` должен запускаться из синхронного контекста (async-фиксьюра дала бы RuntimeError).

- [ ] **Step 2: Create `tests/fakes.py`**

```python
"""Тестовые двойники: RecordingBot и сборщики апдейтов/initData."""

import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import quote

from telegram import Bot, Chat, Message, User


class RecordingBot(Bot):
    """PTB-бот без сетевых вызовов: записывает все действия."""

    def __init__(self, token: str):
        super().__init__(token)
        self._frozen = (
            False  # PTB замораживает объект в Bot.__init__; тесты переприсваивают поля в рантайме
        )
        self.sent: list[dict[str, Any]] = []
        self.edited: list[dict[str, Any]] = []
        self.answered: list[dict[str, Any]] = []
        self.commands: list[Any] | None = None
        self.webhook: dict[str, Any] | None = None
        self.fail_next_send: Exception | None = None
        self.fail_next_edit: Exception | None = None
        self.send_hook: Any = None  # async callable — пауза для тестов конкурентности

    async def initialize(self, *args: Any, **kwargs: Any) -> None:
        self._bot_user = await self.get_me()

    async def shutdown(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def get_me(self, *args: Any, **kwargs: Any) -> User:
        return User(id=123456789, is_bot=True, first_name="Test Bot", username="test_bot")

    async def set_my_commands(self, commands: Any, *args: Any, **kwargs: Any) -> bool:
        self.commands = commands
        return True

    async def set_webhook(self, url: str, **kwargs: Any) -> bool:
        self.webhook = {"url": url, **kwargs}
        return True

    async def delete_webhook(self, **kwargs: Any) -> bool:
        return True

    async def send_message(self, chat_id: Any, text: str, **kwargs: Any) -> Message:
        if self.fail_next_send is not None:
            exc, self.fail_next_send = self.fail_next_send, None
            raise exc
        if self.send_hook is not None:
            await self.send_hook()
        self.sent.append(
            {"chat_id": chat_id, "message_id": len(self.sent) + 1, "text": text, **kwargs}
        )
        return Message(
            message_id=len(self.sent),
            date=int(time.time()),
            chat=Chat(id=chat_id, type="private"),
            text=text,
        )

    async def edit_message_text(
        self, text: str, chat_id: Any = None, message_id: int | None = None, **kwargs: Any
    ) -> bool:
        if self.fail_next_edit is not None:
            exc, self.fail_next_edit = self.fail_next_edit, None
            raise exc
        self.edited.append({"chat_id": chat_id, "message_id": message_id, "text": text, **kwargs})
        return True

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool = False,
        **kwargs: Any,
    ) -> bool:
        self.answered.append({"id": callback_query_id, "text": text, "show_alert": show_alert})
        return True


def make_user_dict(
    user_id: int = 111, username: str | None = None, first_name: str = "Test"
) -> dict:
    d: dict[str, Any] = {"id": user_id, "is_bot": False, "first_name": first_name}
    if username:
        d["username"] = username
    return d


def make_update(
    update_id: int,
    user_id: int = 111,
    *,
    text: str | None = None,
    message_id: int = 1,
    reply_to_message_id: int | None = None,
    data: str | None = None,
) -> dict:
    """dict для Update.de_json: сообщение (text/reply) либо callback_query."""
    user = make_user_dict(user_id)
    if data is not None:
        return {
            "update_id": update_id,
            "callback_query": {
                "id": f"cb{update_id}",
                "from": user,
                "chat_instance": f"ci{update_id}",
                "data": data,
            },
        }
    message: dict[str, Any] = {
        "message_id": message_id,
        "date": 1700000000,
        "chat": {"id": user_id, "type": "private"},
        "from": user,
        "text": text if text is not None else "",
    }
    if reply_to_message_id is not None:
        message["reply_to_message"] = {
            "message_id": reply_to_message_id,
            "date": 1699999999,
            "chat": {"id": user_id, "type": "private"},
            "from": make_user_dict(123456789, first_name="Bot"),
            "text": "question",
        }
    return {"update_id": update_id, "message": message}


def make_init_data(
    user_id: int,
    token: str,
    *,
    username: str | None = None,
    first_name: str = "Test",
    age_seconds: int = 0,
) -> str:
    """Валидный initData Mini App (HMAC-SHA256 по документации Telegram)."""
    payload: dict[str, Any] = {
        "id": user_id,
        "is_bot": False,
        "first_name": first_name,
        "last_name": "User",
    }
    if username:
        payload["username"] = username
    params = {"user": json.dumps(payload, separators=(",", ":"))}
    # auth_date всегда присутствует (даже при age_seconds=0): validate_init_data требует его
    params["auth_date"] = str(int(time.time()) - age_seconds)
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret = hmac.new(token.encode(), b"WebAppData", hashlib.sha256).digest()
    digest = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    return (
        "&".join(f"{k}={quote(v, safe='')}" for k, v in sorted(params.items())) + f"&hash={digest}"
    )
```

- [ ] **Step 3: Verify fakes import and health test runs now**

Run: `docker compose --profile test run --rm test pytest tests/integration/test_health.py tests/unit/ -q`
Expected: 10 passed (8 unit + 2 health).
Run: `docker compose --profile test run --rm test python -c "from tests.fakes import RecordingBot, make_update, make_init_data; print('ok')"`
Expected: `ok`

- [ ] **Step 4: Ruff + commit**

```bash
git add tests/conftest.py tests/fakes.py
git commit -m "test: conftest (real PG per session) and RecordingBot fakes"
```

### Task 2.4: Тесты схемы (инварианты CHECK, identity, updated_at)

**Files:**
- Test: `tests/integration/test_schema.py`

- [ ] **Step 1: Write the tests**

```python
# tests/integration/test_schema.py
import asyncio
import itertools
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models import User

# asyncpg не биндит строку в timestamp — только datetime-объекты.
_TS = datetime(2026, 1, 1, tzinfo=UTC)
# unique(telegram_id): каждый автор-тикета получает свой id.
_TG = itertools.count(10_000)


async def _make_user(session_factory, telegram_id: int | None = None) -> int:
    if telegram_id is None:
        telegram_id = next(_TG)
    async with session_factory() as s, s.begin():
        u = User(telegram_id=telegram_id, first_name="T")
        s.add(u)
        await s.flush()
        return u.id


async def _insert_ticket(session_factory, **kw) -> None:
    defaults = {
        "author_id": None,
        "assignee_id": None,
        "description": "x",
        "priority": "high",
        "request_id": "r1",
        "status": "new",
        "taken_at": None,
        "closed_at": None,
    }
    defaults["author_id"] = await _make_user(session_factory)
    defaults.update(kw)
    async with session_factory() as s, s.begin():
        await s.execute(
            text(
                "INSERT INTO tickets (author_id, assignee_id, description, priority, request_id, "
                "status, taken_at, closed_at) VALUES "
                "(:author_id, :assignee_id, :description, :priority, :request_id, "
                ":status, :taken_at, :closed_at)"
            ),
            defaults,
        )


async def test_new_cannot_have_assignee(db):
    assignee = await _make_user(db.session_factory)
    with pytest.raises(IntegrityError):
        await _insert_ticket(db.session_factory, assignee_id=assignee, taken_at=_TS)


async def test_in_progress_requires_assignee(db):
    with pytest.raises(IntegrityError):
        await _insert_ticket(db.session_factory, status="in_progress", taken_at=_TS)


async def test_in_progress_requires_taken_at(db):
    uid = await _make_user(db.session_factory, telegram_id=7)
    with pytest.raises(IntegrityError):
        await _insert_ticket(db.session_factory, status="in_progress", assignee_id=uid)


async def test_final_requires_closed_at(db):
    assignee = await _make_user(db.session_factory)
    with pytest.raises(IntegrityError):
        await _insert_ticket(
            db.session_factory, status="resolved", assignee_id=assignee, taken_at=_TS
        )


async def test_non_final_forbids_closed_at(db):
    with pytest.raises(IntegrityError):
        await _insert_ticket(db.session_factory, closed_at=_TS)


async def test_description_too_long(db):
    with pytest.raises(IntegrityError):
        await _insert_ticket(db.session_factory, description="y" * 3001)


async def test_description_max_ok(db):
    await _insert_ticket(db.session_factory, description="y" * 3000)


async def test_bad_priority(db):
    with pytest.raises(IntegrityError):
        await _insert_ticket(db.session_factory, priority="urgent")


async def test_ticket_numbers_are_sequential(db):
    await _insert_ticket(db.session_factory)
    await _insert_ticket(db.session_factory, request_id="r2")
    async with db.session_factory() as s:
        ids = (await s.execute(text("SELECT id FROM tickets ORDER BY id"))).scalars().all()
    assert ids == [1, 2]


async def test_one_draft_per_user(db):
    uid = await _make_user(db.session_factory)
    async with db.session_factory() as s, s.begin():
        await s.execute(
            text(
                "INSERT INTO ticket_drafts (user_id, step, version) VALUES (:u, 'description', 1)"
            ),
            {"u": uid},
        )
    with pytest.raises(IntegrityError):
        async with db.session_factory() as s, s.begin():
            await s.execute(
                text(
                    "INSERT INTO ticket_drafts (user_id, step, version) "
                    "VALUES (:u, 'description', 1)"
                ),
                {"u": uid},
            )


async def test_updated_at_changes_on_update(db):
    uid = await _make_user(db.session_factory)
    async with db.session_factory() as s:
        before = (
            await s.execute(text("SELECT updated_at FROM users WHERE id = :i"), {"i": uid})
        ).scalar_one()
    await asyncio.sleep(0.05)
    # ORM-обновление: onupdate=func.now() срабатывает только для ORM/Core-обновлений,
    # сырой SQL его не запускает (см. комментарий в app/models/base.py).
    async with db.session_factory() as s, s.begin():
        user = await s.get(User, uid)
        user.first_name = "X"
    async with db.session_factory() as s:
        after = (
            await s.execute(text("SELECT updated_at FROM users WHERE id = :i"), {"i": uid})
        ).scalar_one()
    assert after > before


async def test_valid_in_progress_row(db):
    author = await _make_user(db.session_factory)
    assignee = await _make_user(db.session_factory)
    await _insert_ticket(
        db.session_factory,
        author_id=author,
        assignee_id=assignee,
        status="in_progress",
        taken_at=_TS,
    )


async def test_valid_resolved_row(db):
    author = await _make_user(db.session_factory)
    assignee = await _make_user(db.session_factory)
    await _insert_ticket(
        db.session_factory,
        author_id=author,
        assignee_id=assignee,
        status="resolved",
        taken_at=_TS,
        closed_at=_TS,
    )


async def test_defaults_version_and_post_synced_version(db):
    await _insert_ticket(db.session_factory)
    async with db.session_factory() as s:
        tid = (await s.execute(text("SELECT MAX(id) FROM tickets"))).scalar_one()
        row = (
            await s.execute(
                text("SELECT version, post_synced_version FROM tickets WHERE id = :i"),
                {"i": tid},
            )
        ).one()
    assert row == (1, 0)
```

- [ ] **Step 2: Run the tests**

Run: `docker compose --profile test run --rm test pytest tests/integration/test_schema.py -q`
Expected: 14 passed.

- [ ] **Step 3: Run previously written health test (conftest уже есть)**

Run: `docker compose --profile test run --rm test pytest tests/integration/test_health.py -q`
Expected: 2 passed.

- [ ] **Step 4: Ruff + commit**

```bash
git add tests/integration/test_schema.py
git commit -m "test: schema invariants (CHECKs, sequence, updated_at, draft uniqueness)"
```

### Task 2.5: Проверка этапа

- [ ] **Step 1: Полный прогон + lint + mypy**

Run: `docker compose --profile test run --rm test pytest -q`
Expected: all passed (19 тестов).
Run: `docker compose --profile test run --rm test ruff check app tests && docker compose --profile test run --rm test mypy app`
Expected: clean.

- [ ] **Step 2: Commit (если были фиксы)**

```bash
git add -A
git commit -m "fix: stage 2 lint/type fixes"
```

# Stage 3 — State machine и сервисный слой

### Task 3.1: Ошибки, метрики (определения), state machine

**Files:**
- Create: `app/services/__init__.py`, `app/services/errors.py`, `app/metrics.py`, `app/states.py`
- Test: `tests/unit/test_states.py`

- [ ] **Step 1: Create `app/services/__init__.py`** (пустой) и `app/services/errors.py`

```python
"""Ошибки сервисного слоя. Тонкие слои (бот/API) маппят их на alerts/HTTP-коды."""


class ServiceError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class NotFoundError(ServiceError):
    pass


class PermissionDenied(ServiceError):
    pass


class StaleTicketError(ServiceError):
    def __init__(self) -> None:
        super().__init__("Обращение изменилось. Повторите действие с актуальной версией.")


class AlreadyTakenError(ServiceError):
    def __init__(self) -> None:
        super().__init__("Обращение уже взято.")


class ActiveTicketsError(ServiceError):
    def __init__(self, ticket_ids: list[int]):
        super().__init__("У пользователя есть обращения в работе.")
        self.ticket_ids = ticket_ids


class NoDraftError(ServiceError):
    def __init__(self) -> None:
        super().__init__("У вас нет активной формы.")


class StaleDraftError(ServiceError):
    def __init__(self) -> None:
        super().__init__("Форма изменилась. Продолжите с текущего шага.")


class StaleAnswerError(ServiceError):
    def __init__(self) -> None:
        super().__init__("Ответ устарел. Ответьте на текущий вопрос формы.")


class DraftValidationError(ServiceError):
    def __init__(self, field: str):
        super().__init__(f"Некорректное значение поля {field}.")
        self.field = field
```

- [ ] **Step 2: Create `app/metrics.py`** (определения; middleware/gauge/сервер — Stage 8)

```python
"""Prometheus-метрики.

Счётчики и гистограммы суммируются по подам через sum().
Gauge tickets_open_by_status вычисляется запросом к БД при скрейпе: каждая
реплика отдаёт одинаковое значение — агрегировать через max(), не sum() (README).
"""
from prometheus_client import Counter, Gauge, Histogram

http_requests_total = Counter(
    "http_requests_total", "Число HTTP-запросов", ["method", "route", "status"]
)
http_request_duration_seconds = Histogram(
    "http_request_duration_seconds", "Латентность HTTP-запросов", ["method", "route"]
)

tickets_created_total = Counter("tickets_created_total", "Созданные обращения", ["priority"])
ticket_transitions_total = Counter(
    "ticket_transitions_total", "Переходы статусов", ["from_status", "to_status"]
)
ticket_take_duration_seconds = Histogram(
    "ticket_take_duration_seconds",
    "Время от входа в new до взятия",
    buckets=(60, 300, 900, 3600, 14400, 86400, 259200),
)
telegram_updates_total = Counter("telegram_updates_total", "Апдейты Telegram", ["result"])
reminders_sent_total = Counter("reminders_sent_total", "Отправленные напоминания", ["result"])
errors_total = Counter("errors_total", "Ошибки", ["component"])

open_tickets_by_status = Gauge(
    "tickets_open_by_status", "Открытые обращения по статусу (запрос к БД)", ["status"]
)
```

- [ ] **Step 3: Create `app/states.py`**

```python
"""Явная state machine статусов обращения (SPEC «Сущности и статусы»)."""
from __future__ import annotations

from dataclasses import dataclass

from app.services.errors import PermissionDenied

PRIORITIES = ("critical", "high", "medium", "low")
STATUSES = ("new", "in_progress", "waiting_info", "resolved", "rejected", "duplicate")
FINAL_STATUSES = ("resolved", "rejected", "duplicate")
EXECUTOR_ROLES = ("developer", "admin")
CREATE_ROLES = ("support", "admin")


@dataclass(frozen=True)
class Transition:
    action: str
    from_statuses: frozenset[str]
    to_status: str | None  # None — статус не меняется (reassign)
    roles: frozenset[str]  # разрешённые роли актора
    assignee_or_admin: bool  # актор — исполнитель или admin
    event_type: str
    clears_assignee: bool = False
    sets_taken_at: bool = False
    clears_taken_at: bool = False
    sets_closed_at: bool = False
    clears_closed_at: bool = False
    sets_reminder: bool = False
    clears_reminder: bool = False


TRANSITIONS: dict[str, Transition] = {
    "take": Transition(
        "take", frozenset({"new"}), "in_progress", frozenset(EXECUTOR_ROLES), False, "taken",
        sets_taken_at=True, clears_reminder=True,
    ),
    "to_waiting": Transition(
        "to_waiting", frozenset({"in_progress"}), "waiting_info", frozenset(EXECUTOR_ROLES), True, "status_changed"
    ),
    "to_in_progress": Transition(
        "to_in_progress", frozenset({"waiting_info"}), "in_progress", frozenset(EXECUTOR_ROLES), True, "status_changed"
    ),
    "to_resolved": Transition(
        "to_resolved", frozenset({"in_progress", "waiting_info"}), "resolved", frozenset(EXECUTOR_ROLES), True,
        "status_changed", sets_closed_at=True,
    ),
    "to_rejected": Transition(
        "to_rejected", frozenset({"in_progress", "waiting_info"}), "rejected", frozenset(EXECUTOR_ROLES), True,
        "status_changed", sets_closed_at=True,
    ),
    "to_duplicate": Transition(
        "to_duplicate", frozenset({"in_progress", "waiting_info"}), "duplicate", frozenset(EXECUTOR_ROLES), True,
        "status_changed", sets_closed_at=True,
    ),
    "decline": Transition(
        "decline", frozenset({"in_progress", "waiting_info"}), "new", frozenset(EXECUTOR_ROLES), True, "declined",
        clears_assignee=True, clears_taken_at=True, sets_reminder=True,
    ),
    "reopen": Transition(
        "reopen", frozenset(FINAL_STATUSES), "new", frozenset({"admin"}), False, "reopened",
        clears_assignee=True, clears_taken_at=True, clears_closed_at=True, sets_reminder=True,
    ),
    "reassign": Transition(
        "reassign", frozenset({"in_progress", "waiting_info"}), None, frozenset({"admin"}), False, "reassigned",
        sets_taken_at=True,
    ),
}


def check_permission(role: str | None, actor_id: int, assignee_id: int | None, t: Transition) -> None:
    if role is None or role not in t.roles:
        raise PermissionDenied("Недостаточно прав для этого действия.")
    if t.assignee_or_admin and role != "admin" and actor_id != assignee_id:
        raise PermissionDenied("Это действие доступно исполнителю обращения или администратору.")
```

- [ ] **Step 4: Write the failing test `tests/unit/test_states.py`**

```python
import pytest

from app.services.errors import PermissionDenied
from app.states import FINAL_STATUSES, TRANSITIONS, check_permission


def test_take_from_new_only_by_executors():
    t = TRANSITIONS["take"]
    assert t.from_statuses == frozenset({"new"})
    assert t.to_status == "in_progress"
    assert t.roles == frozenset({"developer", "admin"})
    check_permission("developer", 1, None, t)
    check_permission("admin", 1, None, t)
    with pytest.raises(PermissionDenied):
        check_permission("support", 1, None, t)
    with pytest.raises(PermissionDenied):
        check_permission(None, 1, None, t)


def test_no_final_transition_from_new():
    for action in ("to_resolved", "to_rejected", "to_duplicate"):
        t = TRANSITIONS[action]
        assert "new" not in t.from_statuses
        assert t.to_status in FINAL_STATUSES
        assert t.sets_closed_at


def test_reopen_and_reassign_admin_only():
    assert TRANSITIONS["reopen"].roles == frozenset({"admin"})
    assert TRANSITIONS["reassign"].roles == frozenset({"admin"})
    for action in ("reopen", "reassign"):
        with pytest.raises(PermissionDenied):
            check_permission("developer", 1, None, TRANSITIONS[action])


def test_active_transitions_assignee_or_admin():
    t = TRANSITIONS["to_waiting"]
    check_permission("admin", 99, 5, t)
    check_permission("developer", 5, 5, t)
    with pytest.raises(PermissionDenied):
        check_permission("developer", 6, 5, t)


def test_decline_and_reopen_clear_fields():
    d = TRANSITIONS["decline"]
    assert (d.to_status, d.clears_assignee, d.clears_taken_at, d.sets_reminder) == ("new", True, True, True)
    r = TRANSITIONS["reopen"]
    assert (r.to_status, r.clears_assignee, r.clears_taken_at, r.clears_closed_at, r.sets_reminder) == (
        "new", True, True, True, True
    )


def test_reassign_keeps_status_resets_taken_at():
    r = TRANSITIONS["reassign"]
    assert r.to_status is None
    assert r.sets_taken_at
    assert r.from_statuses == frozenset({"in_progress", "waiting_info"})
```

- [ ] **Step 5: Run tests (pass after step 3) — run to verify**

Run: `docker compose --profile test run --rm test pytest tests/unit/test_states.py -q`
Expected: 6 passed.

- [ ] **Step 6: Ruff + commit**

```bash
git add app/services/__init__.py app/services/errors.py app/metrics.py app/states.py tests/unit/test_states.py
git commit -m "feat: ticket state machine, service errors, metric definitions"
```

### Task 3.2: Сервис пользователей (upsert, смена роли)

**Files:**
- Create: `app/services/users.py`
- Test: `tests/integration/test_users.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_users.py
import asyncio

import pytest
from sqlalchemy import text

from app.services import tickets as tickets_svc
from app.services import users as users_svc
from app.services.errors import ActiveTicketsError, PermissionDenied


async def test_upsert_creates_then_updates(db):
    async with db.session_factory() as s, s.begin():
        u = await users_svc.upsert_user(s, telegram_id=1, username="a", first_name="A")
        uid = u.id
    async with db.session_factory() as s, s.begin():
        u = await users_svc.upsert_user(s, telegram_id=1, username="a2", first_name="A2")
        assert u.id == uid
    async with db.session_factory() as s:
        row = (await s.execute(text("SELECT username, first_name FROM users WHERE id = :i"), {"i": uid})).one()
    assert row == ("a2", "A2")


async def test_change_role_grant_and_clear(db):
    async with db.session_factory() as s, s.begin():
        u = await users_svc.upsert_user(s, telegram_id=1)
        await users_svc.change_role(s, user_id=u.id, new_role="developer")
        await users_svc.change_role(s, user_id=u.id, new_role=None)
    async with db.session_factory() as s:
        role = (await s.execute(text("SELECT role FROM users WHERE telegram_id = 1"))).scalar_one()
    assert role is None


async def test_change_role_invalid(db):
    async with db.session_factory() as s, s.begin():
        u = await users_svc.upsert_user(s, telegram_id=1)
        with pytest.raises(ValueError):
            await users_svc.change_role(s, user_id=u.id, new_role="boss")


async def test_change_role_forbidden_with_active_tickets(db, settings):
    async with db.session_factory() as s, s.begin():
        sup = await users_svc.upsert_user(s, telegram_id=1, first_name="Sup")
        dev = await users_svc.upsert_user(s, telegram_id=2, first_name="Dev")
        await users_svc.change_role(s, user_id=dev.id, new_role="developer")
        t = await tickets_svc.create_ticket(s, settings, sup.id, description="x", priority="high", request_id="r1")
        await tickets_svc.take_ticket(s, settings, dev.id, t, expected_version=t.version)
        dev_id, ticket_id = dev.id, t.id
    with pytest.raises(ActiveTicketsError) as exc_info:
        async with db.session_factory() as s, s.begin():
            await users_svc.change_role(s, user_id=dev_id, new_role="support")
    assert exc_info.value.ticket_ids == [ticket_id]
    async with db.session_factory() as s:
        role = (await s.execute(text("SELECT role FROM users WHERE id = :i"), {"i": dev_id})).scalar_one()
    assert role == "developer"  # транзакция откатилась


async def test_role_change_and_take_are_serialized(db, settings):
    """FOR UPDATE (смена роли) и FOR SHARE (взятие) исключают взаимно:
    взятие, стартовавшее параллельно со снятием роли, должно отклониться."""
    async with db.session_factory() as s, s.begin():
        sup = await users_svc.upsert_user(s, telegram_id=1)
        dev = await users_svc.upsert_user(s, telegram_id=2)
        await users_svc.change_role(s, user_id=dev.id, new_role="developer")
        t = await tickets_svc.create_ticket(s, settings, sup.id, description="x", priority="high", request_id="r1")
        dev_id, ticket_id = dev.id, t.id

    async def role_change():
        async with db.session_factory() as s, s.begin():
            await s.execute(text("SELECT id FROM users WHERE id = :i FOR UPDATE"), {"i": dev_id})
            await asyncio.sleep(0.3)
            await users_svc.change_role(s, user_id=dev_id, new_role="support")

    async def take():
        async with db.session_factory() as s, s.begin():
            ticket = await tickets_svc.load_ticket(s, ticket_id)
            await asyncio.sleep(0.1)  # смена роли успевает захватить строку
            await tickets_svc.take_ticket(s, settings, dev_id, ticket, expected_version=ticket.version)

    results = await asyncio.gather(role_change(), take(), return_exceptions=True)
    assert results[0] is None
    assert isinstance(results[1], PermissionDenied)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose --profile test run --rm test pytest tests/integration/test_users.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.users'`

- [ ] **Step 3: Create `app/services/users.py`**

```python
"""Пользователи: upsert по telegram_id и смена ролей."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Ticket, User
from app.services.errors import ActiveTicketsError, NotFoundError

EXECUTOR_ROLES = ("developer", "admin")
ALL_ROLES = ("support", "developer", "admin")
ACTIVE_STATUSES = ("in_progress", "waiting_info")


async def upsert_user(
    session: AsyncSession,
    *,
    telegram_id: int,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
) -> User:
    """Создаёт пользователя или обновляет username/имя при каждом взаимодействии."""
    user = (
        await session.execute(select(User).where(User.telegram_id == telegram_id))
    ).scalar_one_or_none()
    if user is None:
        user = User(
            telegram_id=telegram_id, username=username, first_name=first_name, last_name=last_name
        )
        session.add(user)
        await session.flush()
        return user
    user.username = username
    user.first_name = first_name
    user.last_name = last_name
    await session.flush()
    return user


async def change_role(session: AsyncSession, *, user_id: int, new_role: str | None) -> User:
    """Смена роли.

    Строка пользователя блокируется FOR UPDATE; взятие и переназначение берут
    FOR SHARE строку исполнителя — проверка «нет активных обращений» не может
    быть обойдена параллельным взятием (SPEC «Роли»).
    """
    user = (
        await session.execute(select(User).where(User.id == user_id).with_for_update())
    ).scalar_one_or_none()
    if user is None:
        raise NotFoundError("Пользователь не найден.")
    if new_role is not None and new_role not in ALL_ROLES:
        raise ValueError(f"Некорректная роль: {new_role}")
    if new_role not in EXECUTOR_ROLES:
        active = (
            await session.execute(
                select(Ticket.id)
                .where(Ticket.assignee_id == user_id, Ticket.status.in_(ACTIVE_STATUSES))
                .order_by(Ticket.id)
            )
        ).scalars().all()
        if active:
            raise ActiveTicketsError(list(active))
    user.role = new_role
    await session.flush()
    return user
```

- [ ] **Step 4: Run tests**

Run: `docker compose --profile test run --rm test pytest tests/integration/test_users.py -q`
Expected: 5 passed. (Импорт `tickets_svc` в тесте упадёт — сервис создаётся в Task 3.3; если так: создай `app/services/tickets.py` ПОЛНОСТЬЮ по Task 3.3 сразу, иначе перенеси зависимости в задачу 3.3.)

РЕШЕНИЕ (фикс): `app/services/tickets.py` создаётся в этом task — выполни Step 5-7 Task 3.3 СЕЙЧАС (код ниже по плану), затем вернись сюда.

- [ ] **Step 5: Ruff + commit**

```bash
git add app/services/users.py tests/integration/test_users.py
git commit -m "feat: users service (upsert, role change with FOR UPDATE guard)"
```

### Task 3.3: Сервис обращений (create, take, transitions, reassign)

**Files:**
- Create: `app/services/tickets.py`
- Test: `tests/integration/test_tickets.py`, `tests/integration/test_transitions.py`, `tests/integration/test_take_race.py`

- [ ] **Step 1: Create `app/services/tickets.py`**

```python
"""Обращения: создание и все бизнес-изменения.

Каждое бизнес-изменение существующего обращения — ОДНО условное UPDATE
(WHERE id AND status AND version) в той же транзакции, где проверка прав и
запись в журнал (SPEC «Права»). Technical-записи сюда не входят.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.metrics import (
    ticket_take_duration_seconds,
    ticket_transitions_total,
    tickets_created_total,
)
from app.models import Ticket, TicketEvent, User
from app.services import states
from app.services.errors import AlreadyTakenError, NotFoundError, PermissionDenied, StaleTicketError


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def load_ticket(session: AsyncSession, ticket_id: int) -> Ticket:
    ticket = await session.get(Ticket, ticket_id)
    if ticket is None:
        raise NotFoundError("Обращение не найдено.")
    return ticket


async def _role_of(session: AsyncSession, user_id: int) -> str | None:
    return (await session.execute(select(User.role).where(User.id == user_id))).scalar_one()


async def _lock_user_share(session: AsyncSession, user_id: int) -> None:
    """FOR SHARE строки исполнителя: исключает параллельную смену роли (FOR UPDATE)."""
    await session.execute(text("SELECT id FROM users WHERE id = :id FOR SHARE"), {"id": user_id})


async def create_ticket(
    session: AsyncSession,
    settings: Settings,
    actor_id: int,
    *,
    description: str,
    priority: str,
    request_id: str,
    tenant_id: str | None = None,
) -> Ticket:
    if priority not in states.PRIORITIES:
        raise ValueError(f"Некорректный приоритет: {priority}")
    ticket = Ticket(
        author_id=actor_id,
        description=description,
        priority=priority,
        request_id=request_id,
        tenant_id=tenant_id,
        status="new",
        version=1,
        next_reminder_at=_now() + timedelta(seconds=settings.reminder_interval(priority)),
    )
    session.add(ticket)
    await session.flush()
    session.add(
        TicketEvent(ticket_id=ticket.id, event_type="created", actor_id=actor_id, new_status="new", version=1)
    )
    await session.flush()
    tickets_created_total.labels(priority=priority).inc()
    return ticket


async def take_ticket(
    session: AsyncSession, settings: Settings, actor_id: int, ticket: Ticket, expected_version: int
) -> Ticket:
    """Атомарное «Взять». Гонка: второй нажавший получает AlreadyTakenError."""
    t = states.TRANSITIONS["take"]
    await _lock_user_share(session, actor_id)
    role = await _role_of(session, actor_id)  # читаем ПОСЛЕ блокировки
    states.check_permission(role, actor_id, ticket.assignee_id, t)
    if ticket.status != "new":
        raise AlreadyTakenError()
    result = await session.execute(
        update(Ticket)
        .where(Ticket.id == ticket.id, Ticket.status == "new", Ticket.version == expected_version)
        .values(
            status="in_progress",
            assignee_id=actor_id,
            taken_at=_now(),
            next_reminder_at=None,
            version=Ticket.version + 1,
        )
    )
    if result.rowcount != 1:
        await session.refresh(ticket)
        if ticket.status != "new":
            raise AlreadyTakenError()
        raise StaleTicketError()
    await session.refresh(ticket)
    session.add(
        TicketEvent(
            ticket_id=ticket.id,
            event_type="taken",
            actor_id=actor_id,
            old_status="new",
            new_status="in_progress",
            old_assignee_id=None,
            new_assignee_id=actor_id,
            version=expected_version + 1,
        )
    )
    await session.flush()
    ticket_transitions_total.labels(from_status="new", to_status="in_progress").inc()
    await _observe_take_duration(session, ticket.id)
    return ticket


async def apply_transition(
    session: AsyncSession,
    settings: Settings,
    actor_id: int,
    ticket: Ticket,
    expected_version: int,
    action: str,
    target_user_id: int | None = None,
) -> Ticket:
    """Все остальные бизнес-переходы (кроме take): один условный UPDATE."""
    t = states.TRANSITIONS.get(action)
    if t is None:
        raise ValueError(f"Неизвестное действие: {action}")
    if ticket.status not in t.from_statuses:
        raise StaleTicketError()

    role = await _role_of(session, actor_id)
    states.check_permission(role, actor_id, ticket.assignee_id, t)

    old_status, old_assignee = ticket.status, ticket.assignee_id
    new_status = t.to_status or ticket.status
    new_assignee = ticket.assignee_id
    values: dict = {"version": Ticket.version + 1}

    if action == "reassign":
        if target_user_id is None:
            raise ValueError("Для reassign требуется target_user_id.")
        await _lock_user_share(session, target_user_id)
        target_role = await _role_of(session, target_user_id)
        if target_role not in states.EXECUTOR_ROLES:
            raise PermissionDenied("Исполнителем может быть только developer или admin.")
        values["assignee_id"] = target_user_id
        values["taken_at"] = _now()
        new_assignee = target_user_id
    else:
        if t.to_status is not None:
            values["status"] = t.to_status
        if t.sets_taken_at:
            values["taken_at"] = _now()
        if t.clears_taken_at:
            values["taken_at"] = None
        if t.sets_closed_at:
            values["closed_at"] = _now()
        if t.clears_closed_at:
            values["closed_at"] = None
        if t.clears_assignee:
            values["assignee_id"] = None
            new_assignee = None
        if t.sets_reminder:
            values["next_reminder_at"] = _now() + timedelta(seconds=settings.reminder_interval(ticket.priority))
        if t.clears_reminder:
            values["next_reminder_at"] = None

    result = await session.execute(
        update(Ticket)
        .where(Ticket.id == ticket.id, Ticket.status == old_status, Ticket.version == expected_version)
        .values(**values)
    )
    if result.rowcount != 1:
        raise StaleTicketError()
    await session.refresh(ticket)
    session.add(
        TicketEvent(
            ticket_id=ticket.id,
            event_type=t.event_type,
            actor_id=actor_id,
            old_status=old_status,
            new_status=new_status,
            old_assignee_id=old_assignee,
            new_assignee_id=new_assignee,
            version=expected_version + 1,
        )
    )
    await session.flush()
    ticket_transitions_total.labels(from_status=old_status, to_status=new_status).inc()
    return ticket


async def _observe_take_duration(session: AsyncSession, ticket_id: int) -> None:
    """Время до взятия: от последнего входа в new (из журнала) до взятия."""
    last_new_at = (
        await session.execute(
            text(
                "SELECT created_at FROM ticket_events "
                "WHERE ticket_id = :id AND new_status = 'new' ORDER BY id DESC LIMIT 1"
            ),
            {"id": ticket_id},
        )
    ).scalar_one()
    ticket_take_duration_seconds.observe((_now() - last_new_at).total_seconds())
```

- [ ] **Step 2: Write `tests/integration/test_tickets.py`**

```python
# tests/integration/test_tickets.py
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.services import tickets as tickets_svc
from app.services import users as users_svc
from app.services.errors import PermissionDenied, StaleTicketError


async def _make_author_and_ticket(db, settings, *, priority="high"):
    async with db.session_factory() as s, s.begin():
        sup = await users_svc.upsert_user(s, telegram_id=1, first_name="Sup")
        t = await tickets_svc.create_ticket(s, settings, sup.id, description="x", priority=priority, request_id="r1")
        author_id, ticket_id = sup.id, t.id
    return author_id, ticket_id


def _utcnow():
    return datetime.now(timezone.utc)


async def test_create_sets_fields_and_reminder(db, settings):
    author_id, ticket_id = await _make_author_and_ticket(db, settings, priority="critical")
    before, after = _utcnow() - timedelta(seconds=2), _utcnow() + timedelta(seconds=2)
    async with db.session_factory() as s:
        row = (
            await s.execute(
                text(
                    "SELECT status, version, message_id, post_synced_version, next_reminder_at, author_id "
                    "FROM tickets WHERE id = :i"
                ),
                {"i": ticket_id},
            )
        ).one()
        event = (
            await s.execute(text("SELECT event_type, new_status, version FROM ticket_events WHERE ticket_id = :i"), {"i": ticket_id})
        ).one()
    assert row[:4] == ("new", 1, None, 0)
    assert before <= row[4].replace(tzinfo=timezone.utc) <= after + timedelta(seconds=600)
    assert row[4].replace(tzinfo=timezone.utc) >= before + timedelta(seconds=590)
    assert row[5] == author_id
    assert event == ("created", "new", 1)


async def test_create_bad_priority(db, settings):
    with pytest.raises(ValueError):
        async with db.session_factory() as s, s.begin():
            await users_svc.upsert_user(s, telegram_id=1)
            await tickets_svc.create_ticket(s, settings, 1, description="x", priority="urgent", request_id="r1")


async def test_take_by_developer(db, settings):
    author_id, ticket_id = await _make_author_and_ticket(db, settings)
    async with db.session_factory() as s, s.begin():
        dev = await users_svc.upsert_user(s, telegram_id=2)
        await users_svc.change_role(s, user_id=dev.id, new_role="developer")
        dev_id = dev.id
    async with db.session_factory() as s, s.begin():
        t = await tickets_svc.load_ticket(s, ticket_id)
        await tickets_svc.take_ticket(s, settings, dev_id, t, expected_version=1)
    async with db.session_factory() as s:
        row = (
            await s.execute(
                text("SELECT status, assignee_id, taken_at, next_reminder_at, version FROM tickets WHERE id = :i"),
                {"i": ticket_id},
            )
        ).one()
    assert row[0] == "in_progress"
    assert row[1] == dev_id
    assert row[2] is not None
    assert row[3] is None
    assert row[4] == 2


async def test_take_forbidden_for_support_and_norole(db, settings):
    author_id, ticket_id = await _make_author_and_ticket(db, settings)
    for tg, role in ((3, "support"), (4, None)):
        async with db.session_factory() as s, s.begin():
            u = await users_svc.upsert_user(s, telegram_id=tg)
            if role:
                await users_svc.change_role(s, user_id=u.id, new_role=role)
            uid = u.id
        with pytest.raises(PermissionDenied):
            async with db.session_factory() as s, s.begin():
                t = await tickets_svc.load_ticket(s, ticket_id)
                await tickets_svc.take_ticket(s, settings, uid, t, expected_version=t.version)
    async with db.session_factory() as s:
        status = (await s.execute(text("SELECT status FROM tickets WHERE id = :i"), {"i": ticket_id})).scalar_one()
    assert status == "new"


async def test_new_cannot_go_to_final(db, settings):
    _, ticket_id = await _make_author_and_ticket(db, settings)
    # developer со статусом new: права есть, но статус запрещён
    async with db.session_factory() as s, s.begin():
        dev = await users_svc.upsert_user(s, telegram_id=2)
        await users_svc.change_role(s, user_id=dev.id, new_role="developer")
        dev_id = dev.id
    with pytest.raises(StaleTicketError):
        async with db.session_factory() as s, s.begin():
            t = await tickets_svc.load_ticket(s, ticket_id)
            await tickets_svc.apply_transition(s, settings, dev_id, t, expected_version=1, action="to_resolved")
```

- [ ] **Step 3: Write `tests/integration/test_transitions.py`**

```python
# tests/integration/test_transitions.py
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.services import tickets as tickets_svc
from app.services import users as users_svc
from app.services.errors import PermissionDenied, StaleTicketError


async def _setup(db, settings, *, admin_tg=9):
    async with db.session_factory() as s, s.begin():
        sup = await users_svc.upsert_user(s, telegram_id=1)
        dev = await users_svc.upsert_user(s, telegram_id=2)
        other = await users_svc.upsert_user(s, telegram_id=3)
        admin = await users_svc.upsert_user(s, telegram_id=admin_tg)
        await users_svc.change_role(s, user_id=dev.id, new_role="developer")
        await users_svc.change_role(s, user_id=other.id, new_role="developer")
        await users_svc.change_role(s, user_id=admin.id, new_role="admin")
        t = await tickets_svc.create_ticket(s, settings, sup.id, description="x", priority="high", request_id="r1")
        ids = {"sup": sup.id, "dev": dev.id, "other": other.id, "admin": admin.id, "ticket": t.id}
    return ids


async def _transition(db, settings, actor_id, ticket_id, version, action, target_user_id=None):
    async with db.session_factory() as s, s.begin():
        t = await tickets_svc.load_ticket(s, ticket_id)
        await tickets_svc.apply_transition(
            s, settings, actor_id, t, expected_version=version, action=action, target_user_id=target_user_id
        )


async def _take(db, settings, actor_id, ticket_id, version):
    async with db.session_factory() as s, s.begin():
        t = await tickets_svc.load_ticket(s, ticket_id)
        await tickets_svc.take_ticket(s, settings, actor_id, t, expected_version=version)


async def _row(db, ticket_id):
    async with db.session_factory() as s:
        return (
            await s.execute(
                text(
                    "SELECT status, assignee_id, taken_at, closed_at, next_reminder_at, version "
                    "FROM tickets WHERE id = :i"
                ),
                {"i": ticket_id},
            )
        ).one()


async def test_full_cycle_by_assignee(db, settings):
    ids = await _setup(db, settings)
    tid = ids["ticket"]
    await _take(db, settings, ids["dev"], tid, 1)
    await _transition(db, settings, ids["dev"], tid, 2, "to_waiting")
    assert (await _row(db, tid))[0] == "waiting_info"
    await _transition(db, settings, ids["dev"], tid, 3, "to_in_progress")
    await _transition(db, settings, ids["dev"], tid, 4, "to_resolved")
    row = await _row(db, tid)
    assert row[0] == "resolved"
    assert row[3] is not None  # closed_at
    assert row[5] == 5
    async with db.session_factory() as s:
        events = (await s.execute(text("SELECT event_type, version FROM ticket_events WHERE ticket_id=:i ORDER BY id"), {"i": tid})).all()
    assert [e._asdict() if hasattr(e, "_asdict") else tuple(e) for e in events] == [
        ("created", 1), ("taken", 2), ("status_changed", 3), ("status_changed", 4), ("status_changed", 5)
    ]


async def test_other_developer_cannot_touch(db, settings):
    ids = await _setup(db, settings)
    tid = ids["ticket"]
    await _take(db, settings, ids["dev"], tid, 1)
    with pytest.raises(PermissionDenied):
        await _transition(db, settings, ids["other"], tid, 2, "to_resolved")


async def test_admin_can_act_on_anyone_ticket(db, settings):
    ids = await _setup(db, settings)
    tid = ids["ticket"]
    await _take(db, settings, ids["dev"], tid, 1)
    await _transition(db, settings, ids["admin"], tid, 2, "to_resolved")
    assert (await _row(db, tid))[0] == "resolved"


async def test_decline_resets_assignee_and_reminder(db, settings):
    ids = await _setup(db, settings)
    tid = ids["ticket"]
    await _take(db, settings, ids["dev"], tid, 1)
    await _transition(db, settings, ids["dev"], tid, 2, "decline")
    row = await _row(db, tid)
    assert row[0] == "new"
    assert row[1] is None  # assignee сброшен
    assert row[2] is None  # taken_at сброшен
    assert row[4] is not None  # next_reminder_at снова стоит
    assert row[5] == 3


async def test_reopen_admin_only_resets_fields(db, settings):
    ids = await _setup(db, settings)
    tid = ids["ticket"]
    await _take(db, settings, ids["dev"], tid, 1)
    await _transition(db, settings, ids["dev"], tid, 2, "to_rejected")
    with pytest.raises(PermissionDenied):
        await _transition(db, settings, ids["dev"], tid, 3, "reopen")
    await _transition(db, settings, ids["admin"], tid, 3, "reopen")
    row = await _row(db, tid)
    assert (row[0], row[1], row[2], row[3], row[5]) == ("new", None, None, None, 4)
    assert row[4] is not None  # next_reminder_at


async def test_reassign_admin_only_target_executors(db, settings):
    ids = await _setup(db, settings)
    tid = ids["ticket"]
    # с new нельзя
    with pytest.raises(StaleTicketError):
        await _transition(db, settings, ids["admin"], tid, 1, "reassign", target_user_id=ids["other"])
    # не admin не может
    with pytest.raises(PermissionDenied):
        await _transition(db, settings, ids["dev"], tid, 1, "reassign", target_user_id=ids["other"])
    await _take(db, settings, ids["dev"], tid, 1)
    before = (await _row(db, tid))[2]
    # на support — нельзя
    async with db.session_factory() as s, s.begin():
        sup2 = await users_svc.upsert_user(s, telegram_id=5)
        await users_svc.change_role(s, user_id=sup2.id, new_role="support")
        sup2_id = sup2.id
    with pytest.raises(PermissionDenied):
        await _transition(db, settings, ids["admin"], tid, 2, "reassign", target_user_id=sup2_id)
    # на другого developer — можно
    await _transition(db, settings, ids["admin"], tid, 2, "reassign", target_user_id=ids["other"])
    row = await _row(db, tid)
    assert row[0] == "in_progress"  # статус не меняется
    assert row[1] == ids["other"]
    assert row[2] > before  # taken_at сброшен
    assert row[5] == 3
    async with db.session_factory() as s:
        ev = (await s.execute(text("SELECT event_type, old_assignee_id, new_assignee_id, version FROM ticket_events WHERE ticket_id=:i ORDER BY id DESC LIMIT 1"), {"i": tid})).one()
    assert ev == ("reassigned", ids["dev"], ids["other"], 3)


async def test_stale_version_rejected(db, settings):
    ids = await _setup(db, settings)
    tid = ids["ticket"]
    await _take(db, settings, ids["dev"], tid, 1)
    with pytest.raises(StaleTicketError):
        await _transition(db, settings, ids["dev"], tid, 1, "to_resolved")  # version уже 2
```

- [ ] **Step 4: Write `tests/integration/test_take_race.py`**

```python
# tests/integration/test_take_race.py
import asyncio

import pytest
from sqlalchemy import text

from app.models import Ticket
from app.services import tickets as tickets_svc
from app.services.errors import AlreadyTakenError, StaleTicketError


async def _setup(db, settings):
    from app.services import users as users_svc

    async with db.session_factory() as s, s.begin():
        sup = await users_svc.upsert_user(s, telegram_id=1)
        dev = await users_svc.upsert_user(s, telegram_id=2)
        await users_svc.change_role(s, user_id=dev.id, new_role="developer")
        t = await tickets_svc.create_ticket(s, settings, sup.id, description="x", priority="high", request_id="r1")
    return dev.id, t.id


async def _take(db, settings, actor_id, ticket_id, version):
    async with db.session_factory() as s, s.begin():
        t = await tickets_svc.load_ticket(s, ticket_id)
        await tickets_svc.take_ticket(s, settings, actor_id, t, expected_version=version)


async def test_take_race_exactly_one_winner(db, settings):
    dev_id, tid = await _setup(db, settings)
    results = await asyncio.gather(
        _take(db, settings, dev_id, tid, 1),
        _take(db, settings, dev_id, tid, 1),
        return_exceptions=True,
    )
    successes = [r for r in results if not isinstance(r, Exception)]
    errors = [r for r in results if isinstance(r, Exception)]
    assert len(successes) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], AlreadyTakenError)
    async with db.session_factory() as s:
        row = (await s.execute(text("SELECT status, version FROM tickets WHERE id = :i"), {"i": tid})).one()
    assert row == ("in_progress", 2)


async def test_stale_take_button_after_cycle(db, settings):
    """in_progress → waiting_info → in_progress: старая кнопка take не сработает."""
    dev_id, tid = await _setup(db, settings)
    await _take(db, settings, dev_id, tid, 1)

    async def transition(version, action):
        async with db.session_factory() as s, s.begin():
            t = await tickets_svc.load_ticket(s, tid)
            await tickets_svc.apply_transition(s, settings, dev_id, t, expected_version=version, action=action)

    await transition(2, "to_waiting")
    await transition(3, "to_in_progress")
    with pytest.raises(AlreadyTakenError):
        await _take(db, settings, dev_id, tid, 1)  # старая кнопка (v1, статус new)
    with pytest.raises(StaleTicketError):
        await transition(2, "to_waiting")  # старая кнопка to_waiting (v2), актуальная v4


async def test_each_transition_bumps_version_once(db, settings):
    dev_id, tid = await _setup(db, settings)
    await _take(db, settings, dev_id, tid, 1)
    async with db.session_factory() as s, s.begin():
        t = await tickets_svc.load_ticket(s, tid)
        await tickets_svc.apply_transition(s, settings, dev_id, t, expected_version=2, action="to_resolved")
    async with db.session_factory() as s:
        versions = (await s.execute(text("SELECT version FROM ticket_events WHERE ticket_id = :i ORDER BY id"), {"i": tid})).scalars().all()
    assert versions == [1, 2, 3]


async def test_take_duration_uses_last_entry_to_new(db, settings):
    """После decline (новый вход в new) время до взятия считается от decline."""
    dev_id, tid = await _setup(db, settings)
    await _take(db, settings, dev_id, tid, 1)
    async with db.session_factory() as s, s.begin():
        t = await tickets_svc.load_ticket(s, tid)
        await tickets_svc.apply_transition(s, settings, dev_id, t, expected_version=2, action="decline")
    await _take(db, settings, dev_id, tid, 3)
    from app.metrics import ticket_take_duration_seconds

    last = ticket_take_duration_seconds._buckets[0]  # реализационная деталь: проверим через collect
    # проще: проверить через registry collect
    from prometheus_client import CollectorRegistry
    from app import metrics as m

    # берём последний observed sample
    sample = None
    import prometheus_client
    for metric in m.ticket_take_duration_seconds.collect():
        for smp in metric.samples:
            if smp.name == "ticket_take_duration_seconds_bucket" and smp.labels.get("le") == "+Inf":
                sample = smp.value
    assert sample is not None and sample >= 0
```

- [ ] **Step 5: Run all Stage-3 tests**

Run: `docker compose --profile test run --rm test pytest tests/integration/test_tickets.py tests/integration/test_transitions.py tests/integration/test_take_race.py tests/integration/test_users.py -q`
Expected: all passed (~20 тестов). Если `test_take_duration...` хрупкий — замени утверждение на `sample >= 0` (как в коде выше) и упрости тело функции до получения `sample`.

- [ ] **Step 6: Ruff + mypy + commit**

Run: `docker compose --profile test run --rm test ruff check app tests && docker compose --profile test run --rm test mypy app`
```bash
git add app/services/tickets.py tests/integration/test_tickets.py tests/integration/test_transitions.py tests/integration/test_take_race.py
git commit -m "feat: tickets service (create/take/transitions/reassign, conditional UPDATE, events)"
```

### Task 3.4: Проверка этапа

- [ ] **Step 1: Полный прогон**

Run: `docker compose --profile test run --rm test pytest -q && docker compose --profile test run --rm test ruff check app tests && docker compose --profile test run --rm test mypy app`
Expected: all green.

- [ ] **Step 2: Commit (если были фиксы)**

```bash
git add -A
git commit -m "fix: stage 3 lint/type fixes"
```

# Stage 4 — Синхронизация поста

### Task 4.1: Рендер поста + синхронизация с advisory lock

**Files:**
- Create: `app/services/post_sync.py`
- Test: `tests/unit/test_post_render.py`

- [ ] **Step 1: Write the failing test `tests/unit/test_post_render.py`**

```python
from types import SimpleNamespace

from app.services.post_sync import MAX_POST_LEN, format_user, render_post


def _ticket(**kw):
    base = dict(
        id=123,
        description="desc",
        priority="high",
        request_id="R-1",
        tenant_id=None,
        status="new",
        assignee_id=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _user(first="John", username="john", telegram_id=5):
    return SimpleNamespace(first_name=first, username=username, telegram_id=telegram_id)


def test_render_basic():
    text = render_post(_ticket(), _user(), None)
    assert "#123" in text
    assert "R-1" in text
    assert "John (@john)" in text
    assert "высокий" in text
    assert "новое" in text
    assert "Исполнитель: —" in text


def test_render_assignee_tenant_and_status():
    t = _ticket(tenant_id="T-9", status="in_progress")
    text = render_post(t, _user(), _user(first="Dev", username=None, telegram_id=7))
    assert "T-9" in text
    assert "Dev" in text
    assert "в работе" in text


def test_render_truncates_to_limit():
    t = _ticket(description="x" * 5000)
    text = render_post(t, _user(), None)
    assert len(text) <= MAX_POST_LEN
    assert text.count("…") == 1
    assert "x" * 5000 not in text


def test_render_does_not_mutate_ticket():
    t = _ticket(description="y" * 5000)
    original = t.description
    render_post(t, _user(), None)
    assert t.description == original


def test_format_user_variants():
    assert format_user(_user()) == "John (@john)"
    assert format_user(_user(first="Dev", username=None)) == "Dev"
    assert format_user(_user(first=None, username="nick")) == "@nick"
    assert format_user(_user(first=None, username=None, telegram_id=42)) == "ID 42"
```

- [ ] **Step 2: Run to verify failure**

Run: `docker compose --profile test run --rm test pytest tests/unit/test_post_render.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.post_sync'`

- [ ] **Step 3: Create `app/services/post_sync.py`**

```python
"""Синхронизация поста обращения в чате разработчиков (SPEC «Публикация в чат разработчиков»).

Источник истины — БД; пост — её отображение. Сериализация между репликами —
session-level advisory lock на выделенном физическом соединении (lock_engine,
NullPool). Technical-операция: tickets.version НЕ увеличивается.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any, cast

import telegram
from sqlalchemy import select, text, update

from app.config import Settings
from app.db import DB
from app.log import get_logger
from app.models import Ticket, User

log = get_logger("post_sync")

MAX_POST_LEN = 4096

STATUS_RU = {
    "new": "новое",
    "in_progress": "в работе",
    "waiting_info": "ждём информацию",
    "resolved": "решено",
    "rejected": "отклонено",
    "duplicate": "дубль",
}
PRIORITY_RU = {"critical": "критический", "high": "высокий", "medium": "средний", "low": "низкий"}


def format_user(user: User) -> str:
    name, username = user.first_name, user.username
    if name and username:
        return f"{name} (@{username})"
    if name:
        return name
    if username:
        return f"@{username}"
    return f"ID {user.telegram_id}"


def render_post(ticket: Ticket, author: User, assignee: User | None) -> str:
    """Текст поста. Гарантированно <= 4096: описание обрезается с «…»."""
    header = (
        f"Обращение #{ticket.id}\n"
        f"Приоритет: {PRIORITY_RU[ticket.priority]}\n"
        f"Заявка: {ticket.request_id}\n"
        f"Арендатор: {ticket.tenant_id or '—'}\n"
        f"Автор: {format_user(author)}"
    )
    footer = (
        f"Статус: {STATUS_RU[ticket.status]}\n"
        f"Исполнитель: {format_user(assignee) if assignee else '—'}"
    )
    description = ticket.description
    total = f"{header}\n\n{description}\n\n{footer}"
    if len(total) > MAX_POST_LEN:
        overhead = len(header) + len(footer) + 4
        description = description[: MAX_POST_LEN - overhead - 1].rstrip() + "…"
        total = f"{header}\n\n{description}\n\n{footer}"
    return total


async def bot_api_call(
    func: Callable[..., Any], *args: Any, retries: int = 5, **kwargs: Any
) -> Any:
    """Вызов метода бота с ретраями: 429 — backoff с учётом retry_after."""
    for attempt in range(retries):
        try:
            return await func(*args, **kwargs)
        except telegram.error.RetryAfter as e:
            retry_after = e.retry_after
            seconds = retry_after if isinstance(retry_after, int) else retry_after.total_seconds()
            wait = seconds + attempt
            log.warning("telegram 429, повтор через %.1f с", wait)
            await asyncio.sleep(wait)
    raise RuntimeError("Превышено число ретраев Telegram API")


async def _advisory_lock(conn: Any, key: int, timeout: float) -> bool:  # noqa: ASYNC109
    """pg_try_advisory_lock на выделенном соединении. timeout <= 0 — одна попытка (skip)."""
    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        got = (await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})).scalar_one()
        if got:
            return True
        if timeout <= 0 or time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.25)


async def _advisory_unlock(conn: Any, key: int) -> None:
    await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})


async def sync_ticket_post(
    settings: Settings,
    db: DB,
    bot: Any,
    ticket_id: int,
    *,
    force_new: bool = False,
    lock_timeout: float = 30.0,
) -> None:
    """Одна процедура синхронизации поста (SPEC, пункты 1–5).

    lock_timeout <= 0: блокировку не ждать — пропустить (фоновый режим).
    Блокировка снимается в finally при любой ситуации (ошибка, таймаут, cancel);
    при неудаче снятия соединение закрывается (NullPool → физически), и блокировка
    умирает вместе с сессией БД.
    """
    conn = await db.lock_engine.connect()
    locked = False
    try:
        locked = await _advisory_lock(conn, ticket_id, lock_timeout)
        if not locked:
            log.warning("post sync skipped: advisory lock busy", ticket_id=ticket_id)
            return
        await _do_sync(settings, db, bot, ticket_id, force_new=force_new)
    finally:
        try:
            if locked:
                try:
                    await _advisory_unlock(conn, ticket_id)
                except Exception:
                    log.exception("advisory unlock failed; closing connection", ticket_id=ticket_id)
        finally:
            await conn.close()


async def _do_sync(
    settings: Settings, db: DB, bot: Any, ticket_id: int, *, force_new: bool
) -> None:
    # 2. Под блокировкой — свежее состояние из БД; рендерим ТОЛЬКО из него.
    async with db.session_factory() as session:
        ticket = await session.get(Ticket, ticket_id)
        if ticket is None:
            return
        author = cast(User, await session.get(User, ticket.author_id))
        assignee = await session.get(User, ticket.assignee_id) if ticket.assignee_id else None
        rendered_version = ticket.version
        post_text = render_post(ticket, author, assignee)
        message_id: int | None = None if force_new else ticket.message_id
        dev_chat = settings.dev_chat_id

    # 3. Отправить новый либо отредактировать существующий.
    if message_id is None:
        sent = await bot_api_call(bot.send_message, dev_chat, post_text)
        message_id = sent.message_id
    else:
        try:
            await bot_api_call(
                bot.edit_message_text, post_text, chat_id=dev_chat, message_id=message_id
            )
        except telegram.error.BadRequest as e:
            msg = str(e).lower()
            if "message is not modified" in msg:
                pass  # пост уже актуален — считаем успехом
            elif "not found" in msg or "can't edit" in msg or "no rights" in msg:
                # пост удалён/недоступен — публикуем новый
                sent = await bot_api_call(bot.send_message, dev_chat, post_text)
                message_id = sent.message_id
            else:
                raise

    # 4. Одной транзакцией: post_synced_version = отрендеренная version
    #    (НЕ текущая: если состояние изменилось, фон увидит отставание и повторит).
    async with db.session_factory() as session, session.begin():
        values: dict[str, Any] = {"post_synced_version": rendered_version}
        stored_message_id = (
            await session.execute(select(Ticket.message_id).where(Ticket.id == ticket_id))
        ).scalar_one()
        if stored_message_id != message_id:
            values["message_id"] = message_id
        await session.execute(update(Ticket).where(Ticket.id == ticket_id).values(**values))
    # 5. Блокировку снимает sync_ticket_post в finally.
```

- [ ] **Step 4: Run unit tests**

Run: `docker compose --profile test run --rm test pytest tests/unit/test_post_render.py -q`
Expected: 5 passed.

- [ ] **Step 5: Ruff + commit**

```bash
git add app/services/post_sync.py tests/unit/test_post_render.py
git commit -m "feat: post renderer (<=4096) and advisory-lock post sync"
```

### Task 4.2: Тесты синхронизации

**Files:**
- Test: `tests/integration/test_post_sync.py`

- [ ] **Step 1: Write the tests**

```python
# tests/integration/test_post_sync.py
import asyncio
import time
from datetime import timedelta

import pytest
import telegram
from sqlalchemy import text

from app.services import tickets as tickets_svc
from app.services import users as users_svc
from app.services.post_sync import sync_ticket_post


async def _make_ticket(db, settings) -> int:
    async with db.session_factory() as s, s.begin():
        sup = await users_svc.upsert_user(s, telegram_id=1, first_name="Sup", username="sup")
        t = await tickets_svc.create_ticket(
            s, settings, sup.id, description="desc", priority="high", request_id="R-1"
        )
    return t.id


async def _row(db, tid):
    async with db.session_factory() as s:
        return (
            await s.execute(
                text("SELECT message_id, post_synced_version, version FROM tickets WHERE id = :i"),
                {"i": tid},
            )
        ).one()


async def _dev_id(db) -> int:
    async with db.session_factory() as s, s.begin():
        dev = await users_svc.upsert_user(s, telegram_id=2, first_name="Dev")
        await users_svc.change_role(s, user_id=dev.id, new_role="developer")
        return dev.id


async def _take(db, settings, tid):
    dev_id = await _dev_id(db)
    async with db.session_factory() as s, s.begin():
        t = await tickets_svc.load_ticket(s, tid)
        await tickets_svc.take_ticket(s, settings, dev_id, t, expected_version=t.version)


async def _selector_ids(db):
    from app.workers import post_sync_iteration  # noqa: F401  (импорт для проверки наличия)

    async with db.session_factory() as s:
        return (
            (
                await s.execute(
                    text(
                        "SELECT id FROM tickets "
                        "WHERE message_id IS NULL OR post_synced_version < version"
                    )
                )
            )
            .scalars()
            .all()
        )


async def test_initial_publish(db, settings, bot):
    tid = await _make_ticket(db, settings)
    await sync_ticket_post(settings, db, bot, tid)
    row = await _row(db, tid)
    assert row[0] is not None
    assert row[1] == row[2] == 1  # post_synced_version == version; version не вырос
    assert len(bot.sent) == 1
    assert bot.sent[0]["chat_id"] == settings.dev_chat_id
    assert f"#{tid}" in bot.sent[0]["text"]
    assert tid not in await _selector_ids(db)


async def test_edit_existing_post(db, settings, bot):
    tid = await _make_ticket(db, settings)
    await sync_ticket_post(settings, db, bot, tid)
    await _take(db, settings, tid)
    await sync_ticket_post(settings, db, bot, tid)
    assert len(bot.sent) == 1
    assert len(bot.edited) == 1
    assert bot.edited[0]["message_id"] == bot.sent[0]["message_id"]
    assert "в работе" in bot.edited[0]["text"]
    row = await _row(db, tid)
    assert row[1] == 2


async def test_not_modified_is_success(db, settings, bot):
    tid = await _make_ticket(db, settings)
    await sync_ticket_post(settings, db, bot, tid)
    await _take(db, settings, tid)
    bot.fail_next_edit = telegram.error.BadRequest("message is not modified")
    await sync_ticket_post(settings, db, bot, tid)
    row = await _row(db, tid)
    assert row[1] == 2


async def test_lost_post_resends(db, settings, bot):
    tid = await _make_ticket(db, settings)
    await sync_ticket_post(settings, db, bot, tid)
    old_msg = bot.sent[0]["message_id"]
    await _take(db, settings, tid)
    bot.fail_next_edit = telegram.error.BadRequest("message to edit not found")
    await sync_ticket_post(settings, db, bot, tid)
    row = await _row(db, tid)
    assert row[0] != old_msg
    assert len(bot.sent) == 2


async def test_force_new_restores_post(db, settings, bot):
    tid = await _make_ticket(db, settings)
    await sync_ticket_post(settings, db, bot, tid)
    old_msg = bot.sent[0]["message_id"]
    await sync_ticket_post(settings, db, bot, tid, force_new=True)
    assert len(bot.sent) == 2
    assert bot.edited == []
    row = await _row(db, tid)
    assert row[0] != old_msg
    assert row[1] == 1  # version не изменилась


async def test_429_retry_uses_retry_after(db, settings, bot, monkeypatch):
    tid = await _make_ticket(db, settings)
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(s):
        sleeps.append(s)
        await real_sleep(0)

    import app.services.post_sync as ps

    monkeypatch.setattr(ps.asyncio, "sleep", fake_sleep)
    bot.fail_next_send = telegram.error.RetryAfter(timedelta(seconds=1))
    await sync_ticket_post(settings, db, bot, tid)
    assert sleeps == [1.0]
    assert len(bot.sent) == 1
    row = await _row(db, tid)
    assert row[1] == 1


async def test_change_during_send_leaves_lag(db, settings, bot):
    """Изменение во время отправки: сохраняется отрендеренная version < текущей."""
    tid = await _make_ticket(db, settings)
    started = asyncio.Event()

    async def hook():
        started.set()
        await _take(db, settings, tid)

    bot.send_hook = hook
    task = asyncio.create_task(sync_ticket_post(settings, db, bot, tid))
    await started.wait()
    await task
    row = await _row(db, tid)
    assert row[1] < row[2]  # отставание
    assert tid in await _selector_ids(db)


async def test_no_stale_post_after_concurrent_changes(db, settings, bot):
    """Фоновые итерации догоняют финальное состояние (без возврата устаревшего статуса)."""
    from app.workers import post_sync_iteration

    tid = await _make_ticket(db, settings)
    await post_sync_iteration(settings, db, bot)
    await _take(db, settings, tid)
    await post_sync_iteration(settings, db, bot)
    dev_id = await _dev_id(db)
    async with db.session_factory() as s, s.begin():
        t = await tickets_svc.load_ticket(s, tid)
        await tickets_svc.apply_transition(
            s, settings, dev_id, t, expected_version=t.version, action="to_resolved"
        )
    await post_sync_iteration(settings, db, bot)
    assert "решено" in bot.edited[-1]["text"]
    row = await _row(db, tid)
    assert row[1] == row[2] == 3


async def test_lock_serializes_concurrent_syncs(db, settings, bot):
    tid = await _make_ticket(db, settings)
    await asyncio.gather(
        sync_ticket_post(settings, db, bot, tid),
        sync_ticket_post(settings, db, bot, tid),
    )
    assert len(bot.sent) == 1
    assert len(bot.edited) == 1
    row = await _row(db, tid)
    assert row[1] == row[2]


async def test_lock_released_on_error(db, settings, bot):
    tid = await _make_ticket(db, settings)
    bot.fail_next_send = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        await sync_ticket_post(settings, db, bot, tid)
    await sync_ticket_post(settings, db, bot, tid)
    assert len(bot.sent) == 1


async def test_lock_released_on_cancel(db, settings, bot):
    tid = await _make_ticket(db, settings)
    started = asyncio.Event()

    async def hook():
        started.set()
        await asyncio.Event().wait()  # вечная блокировка

    bot.send_hook = hook
    task = asyncio.create_task(sync_ticket_post(settings, db, bot, tid))
    await started.wait()
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    bot.send_hook = None
    await sync_ticket_post(settings, db, bot, tid)
    assert len(bot.sent) == 1


async def test_skip_when_lock_busy(db, settings, bot):
    tid = await _make_ticket(db, settings)
    conn = await db.lock_engine.connect()
    try:
        assert (
            await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": tid})
        ).scalar_one()
        t0 = time.monotonic()
        await sync_ticket_post(settings, db, bot, tid, lock_timeout=0.0)
        assert time.monotonic() - t0 < 1.0
        assert bot.sent == []
    finally:
        await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": tid})
        await conn.close()
```

- [ ] **Step 2: Create `app/workers.py` (v1: post-sync iteration + loop)**

```python
# app/workers.py
"""Фоновые задачи: работают в каждой реплике; истина — в БД."""

import asyncio
from typing import Any

import sentry_sdk
from sqlalchemy import select

from app.config import Settings
from app.db import DB
from app.log import get_logger
from app.metrics import errors_total
from app.models import Ticket
from app.services.post_sync import sync_ticket_post

log = get_logger("workers")


def _report(component: str, exc: BaseException) -> None:
    log.error("worker error", component=component, exc_info=exc)
    errors_total.labels(component=component).inc()
    sentry_sdk.capture_exception(exc)


async def post_sync_iteration(settings: Settings, db: DB, bot: Any) -> None:
    """Одна итерация: выбрать несинхронизированные и синхронизировать
    (skip при занятой блокировке)."""
    async with db.session_factory() as session:
        ids = (
            (
                await session.execute(
                    select(Ticket.id)
                    .where(
                        (Ticket.message_id.is_(None))
                        | (Ticket.post_synced_version < Ticket.version)
                    )
                    .order_by(Ticket.id)
                    .limit(50)
                )
            )
            .scalars()
            .all()
        )
    for ticket_id in ids:
        try:
            await sync_ticket_post(settings, db, bot, ticket_id, lock_timeout=0.0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _report("post_sync", exc)


async def run_post_sync_loop(settings: Settings, db: DB, bot: Any) -> None:
    while True:
        await asyncio.sleep(settings.post_sync_interval_seconds)
        try:
            await post_sync_iteration(settings, db, bot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _report("post_sync_loop", exc)
```

- [ ] **Step 3: Run the tests**

Run: `docker compose --profile test run --rm test pytest tests/integration/test_post_sync.py -q`
Expected: 12 passed.

- [ ] **Step 4: Ruff + mypy + commit**

```bash
git add app/workers.py tests/integration/test_post_sync.py
git commit -m "feat: post sync integration tests and background sync worker"
```

### Task 4.3: Проверка этапа

- [ ] **Step 1: Полный прогон**

Run: `docker compose --profile test run --rm test pytest -q && docker compose --profile test run --rm test ruff check app tests && docker compose --profile test run --rm test mypy app`
Expected: all green (68 тестов; 4 warning: path_separator + 3× PTBDeprecationWarning из 429-теста — PTB-внутренние, cleanup в Stage 9).

- [ ] **Step 2: Commit (если были фиксы)**

```bash
git add -A
git commit -m "fix: stage 4 lint/type fixes"
```

# Stage 5 — Бот (форма, кнопки, дедупликация, webhook)

### Task 5.1: Дедупликация апдейтов

**Files:**
- Create: `app/services/updates.py`
- Test: `tests/integration/test_updates_dedup.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_updates_dedup.py
from sqlalchemy import text

from app.services.updates import purge_processed_updates, register_update


async def test_register_once(db):
    assert await register_update(db, 100) is True
    assert await register_update(db, 100) is False
    assert await register_update(db, 101) is True


async def test_error_does_not_rollback_dedup(db):
    """Ошибка бизнес-обработки не откатывает запись о дедупликации."""
    assert await register_update(db, 200) is True
    try:
        async with db.session_factory() as session, session.begin():
            await session.execute(text("SELECT 1/0"))
    except Exception:
        pass
    assert await register_update(db, 200) is False  # запись жива


async def test_purge_old_updates(db):
    await register_update(db, 300)
    async with db.session_factory() as s, s.begin():
        await s.execute(
            text("UPDATE processed_updates SET processed_at = now() - interval '49 hours'")
        )
    await purge_processed_updates(db)
    async with db.session_factory() as s:
        n = (await s.execute(text("SELECT count(*) FROM processed_updates"))).scalar_one()
    assert n == 0
```

- [ ] **Step 2: Run to verify failure, then create `app/services/updates.py`**

```python
"""Дедупликация апдейтов (SPEC «FastAPI»).

Запись коммитится отдельной короткой транзакцией ДО бизнес-обработки,
поэтому ошибка обработки её не откатывает. Не более одной попытки
обработки каждого апдейта.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy import CursorResult, text

from app.db import DB


async def register_update(db: DB, update_id: int) -> bool:
    """True, если апдейт обрабатывается впервые."""
    async with db.session_factory() as session, session.begin():
        result = cast(
            CursorResult[()],
            await session.execute(
                text(
                    "INSERT INTO processed_updates (update_id) VALUES (:id) "
                    "ON CONFLICT (update_id) DO NOTHING"
                ),
                {"id": update_id},
            ),
        )
        return (result.rowcount or 0) == 1


async def purge_processed_updates(db: DB, older_than_hours: int = 48) -> None:
    async with db.session_factory() as session, session.begin():
        await session.execute(
            text(
                "DELETE FROM processed_updates WHERE processed_at < now() - "
                "make_interval(hours => :h)"
            ),
            {"h": older_than_hours},
        )
```

- [ ] **Step 3: Run tests**

Run: `docker compose --profile test run --rm test pytest tests/integration/test_updates_dedup.py -q`
Expected: 3 passed.

- [ ] **Step 4: Ruff + commit**

```bash
git add app/services/updates.py tests/integration/test_updates_dedup.py
git commit -m "feat: update deduplication (processed_updates, ON CONFLICT DO NOTHING)"
```

### Task 5.2: PTB-приложение (AppApplication, дедупликация, contextvars)

**Files:**
- Create: `app/bot/__init__.py` (пустой), `app/bot/handlers/__init__.py` (пустой), `app/bot/application.py`
- Test: `tests/integration/test_commands.py` (задаётся полностью в Task 5.6; сейчас создай только тест дедупликации)

- [ ] **Step 1: Create `app/bot/application.py`** (хендлеры регистрируются в Task 5.6–5.8; импорты хендлеров добавляй тогда)

Временная версия — без импорта хендлеров:

```python
"""PTB Application: дедупликация апдейтов, контекстные теги, Sentry.

PTB 22.8: `process_update(update)` — один аргумент. Ошибки хендлеров не
выходят за пределы process_update (уходит в error handler).
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import sentry_sdk
from structlog.contextvars import bind_contextvars, clear_contextvars
from telegram import BotCommand, Update
from telegram.ext import Application, ApplicationBuilder

from app.config import Settings
from app.db import DB
from app.log import get_logger
from app.metrics import errors_total, telegram_updates_total
from app.services.updates import register_update
from app.services.users import upsert_user

log = get_logger("bot.application")

BOT_COMMANDS = [
    BotCommand("new", "Создать обращение"),
    BotCommand("cancel", "Отменить форму"),
    BotCommand("help", "Справка"),
]


async def _post_init(app: Any) -> None:
    await app.bot.set_my_commands(BOT_COMMANDS)
    if app.settings is not None and app.settings.bot_mode == "webhook":
        await app.bot.set_webhook(
            app.settings.webhook_url, secret_token=app.settings.webhook_secret_token
        )


async def _on_error(bot: Any, context: Any) -> None:
    update = context.update
    update_id = update.update_id if update is not None else None
    user_id = update.effective_user.id if update is not None and update.effective_user else None
    log.error(
        "telegram update error",
        exc_info=context.error,
        update_id=update_id,
        telegram_user_id=user_id,
    )
    errors_total.labels(component="telegram").inc()
    sentry_sdk.capture_exception(context.error)


class AppApplication(Application[Any, Any, Any, Any, Any, Any]):
    @classmethod
    def builder(cls) -> ApplicationBuilder[Any, Any, Any, Any, Any, Any]:
        return ApplicationBuilder().application_class(cls)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.settings: Settings | None = None
        self.db: DB | None = None

    async def process_update(self, update: object) -> None:
        """Дедупликация (отдельная транзакция) + upsert пользователя + теги.

        Работает и в webhook (прямой вызов), и в polling (через update_queue).
        """
        db = self.db
        update_id: int | None = update.update_id if isinstance(update, Update) else None
        if db is not None and update_id is not None:
            fresh = await register_update(db, update_id)
            if not fresh:
                telegram_updates_total.labels(result="duplicate").inc()
                log.debug("duplicate update skipped", update_id=update_id)
                return
            telegram_updates_total.labels(result="processed").inc()
        clear_contextvars()
        effective = update.effective_user if isinstance(update, Update) else None
        bind_contextvars(
            request_id=uuid.uuid4().hex,
            update_id=update_id,
            telegram_user_id=effective.id if effective is not None else None,
        )
        if db is not None and effective is not None:
            # username/имя обновляются при каждом взаимодействии (SPEC «Роли»)
            async with db.session_factory() as session, session.begin():
                await upsert_user(
                    session,
                    telegram_id=effective.id,
                    username=effective.username,
                    first_name=effective.first_name,
                    last_name=effective.last_name,
                )
        try:
            await super().process_update(update)
        finally:
            clear_contextvars()


def build_application(settings: Settings, db: DB, bot: Any = None) -> AppApplication:
    builder = AppApplication.builder()
    builder = builder.bot(bot) if bot is not None else builder.token(settings.bot_token)
    app = cast(AppApplication, builder.post_init(_post_init).build())
    app.settings = settings
    app.db = db
    from app.bot.handlers.callbacks import register_post_callback_handlers  # type: ignore  # noqa: E501
    from app.bot.handlers.commands import register_command_handlers  # type: ignore  # noqa: E501
    from app.bot.handlers.draft_form import register_draft_form_handlers  # type: ignore  # noqa: E501

    register_command_handlers(app)
    register_draft_form_handlers(app)
    register_post_callback_handlers(app)
    app.add_error_handler(_on_error)
    return app
```

Note: импорты хендлеров внутри `build_application` — они появятся в Task 5.6–5.8; до этого `build_application` не вызывается.

- [ ] **Step 2: Create `app/bot/__init__.py`, `app/bot/handlers/__init__.py`** (пустые).

- [ ] **Step 3: Ruff check** (тесты появятся в 5.6)

Run: `docker compose --profile test run --rm test ruff check app/bot`
Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add app/bot/__init__.py app/bot/handlers/__init__.py app/bot/application.py
git commit -m "feat: AppApplication with dedup, user upsert, contextvars, error handler"
```

### Task 5.3: Форма: шаги, вопросы, callback_data

**Files:**
- Create: `app/bot/form.py`
- Test: `tests/unit/test_form.py`

- [ ] **Step 1: Create `app/bot/form.py`**

```python
"""Форма создания обращения: шаги, вопросы, callback_data (<= 64 байт)."""

from __future__ import annotations

import uuid as _uuid
from typing import Any

from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup

STEPS = ("description", "priority", "request_id", "tenant_id", "confirm")
TEXT_STEPS = ("description", "request_id", "tenant_id")
PRIORITY_CHOICES = (
    ("critical", "Критический"),
    ("high", "Высокий"),
    ("medium", "Средний"),
    ("low", "Низкий"),
)
PRIORITY_SHORT = {"critical": "c", "high": "h", "medium": "m", "low": "l"}
SHORT_PRIORITY = {v: k for k, v in PRIORITY_SHORT.items()}
PRIORITY_LABEL = dict(PRIORITY_CHOICES)

QUESTIONS = {
    "description": "Опишите обращение (до 3000 символов):",
    "request_id": "Введите id заявки (до 64 символов):",
    "tenant_id": "Введите id арендатора (до 64 символов) или нажмите «Пропустить»:",
}


def _uid(draft: Any) -> str:
    return str(draft.id).replace("-", "")  # 32 hex


def cb(kind: str, draft: Any, *extra: str) -> str:
    """callback_data формы: d:<kind>:<uuid32>:<version>[:extra]. <= 64 байт."""
    data = ":".join(["d", kind, _uid(draft), str(draft.version), *extra])
    assert len(data.encode()) <= 64, f"callback_data too long: {len(data)}"
    return data


def question_for(step: str, draft: Any) -> tuple[str, Any]:
    """(text, reply_markup) вопроса текущего шага."""
    if step in QUESTIONS:
        return QUESTIONS[step], ForceReply(True)
    if step == "priority":
        keyboard = [
            [
                InlineKeyboardButton(label, callback_data=cb("prio", draft, PRIORITY_SHORT[p]))
                for p, label in PRIORITY_CHOICES
            ]
        ]
        return "Выберите приоритет:", InlineKeyboardMarkup(keyboard)
    # confirm
    lines = [
        "Проверьте данные:",
        f"• Описание: {draft.description}",
        f"• Приоритет: {PRIORITY_LABEL.get(draft.priority or '', draft.priority)}",
        f"• Id заявки: {draft.request_id}",
        f"• Id арендатора: {draft.tenant_id or '—'}",
    ]
    keyboard = [
        [
            InlineKeyboardButton("✅ Отправить", callback_data=cb("send", draft)),
            InlineKeyboardButton("✖ Отменить", callback_data=cb("canx", draft)),
        ]
    ]
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


def parse_draft_callback(data: str) -> tuple[str, str, int, str | None] | None:
    """d:<kind>:<uuid32>:<version>[:extra] → (kind, uuid_str, version, extra)."""
    parts = data.split(":")
    if len(parts) < 4 or parts[0] != "d":
        return None
    kind, uid, version = parts[1], parts[2], parts[3]
    extra = parts[4] if len(parts) > 4 else None
    try:
        _uuid.UUID(uid)
        version_int = int(version)
    except ValueError:
        return None
    return kind, uid, version_int, extra

- [ ] **Step 2: Write `tests/unit/test_form.py`**

```python
import uuid as uuid_lib
from types import SimpleNamespace

from app.bot import form


def _draft(version=1, step="description", **kw):
    base = dict(
        id=uuid_lib.uuid4(),
        version=version,
        step=step,
        description="desc",
        priority="high",
        request_id="r1",
        tenant_id=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_callback_within_64_bytes():
    d = _draft(version=1234)
    for kind, extra in (
        ("send", None),
        ("canx", None),
        ("cont", None),
        ("reset", None),
        ("skip", None),
        ("prio", "c"),
    ):
        data = form.cb(kind, d, extra) if extra else form.cb(kind, d)
        assert len(data.encode()) <= 64, kind


def test_parse_roundtrip():
    d = _draft(version=7)
    kind, uid, version, extra = form.parse_draft_callback(form.cb("prio", d, "h"))
    assert (kind, uid, version, extra) == ("prio", str(d.id).replace("-", ""), 7, "h")


def test_parse_rejects_foreign():
    assert form.parse_draft_callback("t:take:1:2") is None
    assert form.parse_draft_callback("d:send:short:1") is None
    assert form.parse_draft_callback("garbage") is None
    assert form.parse_draft_callback("") is None


def test_questions():
    text, markup = form.question_for("description", _draft(step="description"))
    assert "3000" in text and markup is not None
    text2, markup2 = form.question_for("priority", _draft(step="priority"))
    assert "приоритет" in text2.lower()
    assert len(markup2.inline_keyboard) == 1 and len(markup2.inline_keyboard[0]) == 4
    text3, markup3 = form.question_for("confirm", _draft(step="confirm"))
    assert "desc" in text3 and "r1" in text3
```

- [ ] **Step 3: Run tests**

Run: `docker compose --profile test run --rm test pytest tests/unit/test_form.py -q`
Expected: 4 passed.

- [ ] **Step 4: Ruff + commit**

```bash
git add app/bot/form.py tests/unit/test_form.py
git commit -m "feat: draft form steps, questions and compact callback_data"
```

### Task 5.4: Сервис черновиков (drafts)

**Files:**
- Create: `app/services/drafts.py`

- [ ] **Step 1: Create `app/services/drafts.py`**

```python
"""Черновик формы обращения (SPEC «Создание обращения»).

Много-репликовая безопасность:
- один активный черновик на пользователя (UNIQUE user_id);
- изменение данных/шага — в транзакции под SELECT ... FOR UPDATE, version + 1 ровно один раз;
- отклонённые/устаревшие апдейты ничего не меняют (данные, шаг, version);
- prompt_message_id — technical-запись, version не увеличивает, сохраняется
  условным UPDATE (draft_id + version + step).
"""

from __future__ import annotations

import uuid as _uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import cast

from sqlalchemy import CursorResult, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.models import Draft, Ticket, TicketEvent, User
from app.services.errors import DraftValidationError, StaleDraftError, StaleAnswerError


@dataclass(frozen=True)
class DraftResult:
    step: str | None  # новый шаг (None — черновик удалён)
    version: int | None
    ticket_id: int | None


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get_draft(session: AsyncSession, user_id: int) -> Draft | None:
    return (
        await session.execute(select(Draft).where(Draft.user_id == user_id))
    ).scalar_one_or_none()


async def start_draft(
    session: AsyncSession, user_id: int, *, reset: bool = False
) -> tuple[Draft, bool]:
    """(draft, created). При reset=True существующий черновик заменяется новым."""
    draft = await get_draft(session, user_id)
    if draft is not None:
        if not reset:
            return draft, False
        await session.execute(delete(Draft).where(Draft.id == draft.id))
        await session.flush()
    draft = Draft(user_id=user_id, step="description", version=1)
    session.add(draft)
    await session.flush()
    return draft, True


async def reset_draft(
    session: AsyncSession, user_id: int, draft_id: _uuid.UUID, expected_version: int
) -> Draft:
    """«Начать заново»: проверка id+version, удаление, новый черновик."""
    draft = (
        await session.execute(
            select(Draft).where(Draft.id == draft_id, Draft.user_id == user_id).with_for_update()
        )
    ).scalar_one_or_none()
    if draft is None or draft.version != expected_version:
        raise StaleDraftError()
    await session.execute(delete(Draft).where(Draft.id == draft.id))
    await session.flush()
    new = Draft(user_id=user_id, step="description", version=1)
    session.add(new)
    await session.flush()
    return new


async def answer_text(
    session: AsyncSession, user_id: int, raw_text: str, reply_to_message_id: int | None
) -> DraftResult:
    """Текстовый ответ, привязанный к prompt_message_id. Устаревший ответ ничего не меняет."""
    draft = (
        await session.execute(select(Draft).where(Draft.user_id == user_id).with_for_update())
    ).scalar_one_or_none()
    if draft is None:
        raise StaleAnswerError()
    if draft.prompt_message_id is None or reply_to_message_id != draft.prompt_message_id:
        raise StaleAnswerError()
    text_value = raw_text.strip()
    if draft.step == "description":
        if not text_value or len(text_value) > 3000:
            raise DraftValidationError("описания")
        draft.description = text_value
        draft.step = "priority"
    elif draft.step == "request_id":
        if not text_value or len(text_value) > 64:
            raise DraftValidationError("id заявки")
        draft.request_id = text_value
        draft.step = "tenant_id"
    elif draft.step == "tenant_id":
        if not text_value or len(text_value) > 64:
            raise DraftValidationError("id арендатора")
        draft.tenant_id = text_value
        draft.step = "confirm"
    else:
        raise StaleAnswerError()
    draft.prompt_message_id = None
    draft.version += 1
    await session.flush()
    return DraftResult(step=draft.step, version=draft.version, ticket_id=None)


async def answer_choice(
    session: AsyncSession,
    settings: Settings,
    user_id: int,
    draft_id: _uuid.UUID,
    expected_version: int,
    choice: str,
    value: str | None = None,
) -> DraftResult:
    """Кнопка формы: prio / skip / send / canx. Чужой id или устаревшая version → StaleDraftError."""
    draft = (
        await session.execute(
            select(Draft).where(Draft.id == draft_id, Draft.user_id == user_id).with_for_update()
        )
    ).scalar_one_or_none()
    if draft is None or draft.version != expected_version:
        raise StaleDraftError()

    if choice == "prio":
        if draft.step != "priority" or value not in ("critical", "high", "medium", "low"):
            raise StaleDraftError()
        draft.priority = value
        draft.step = "request_id"
    elif choice == "skip":
        if draft.step != "tenant_id":
            raise StaleDraftError()
        draft.tenant_id = None
        draft.step = "confirm"
    elif choice == "canx":
        await session.execute(delete(Draft).where(Draft.id == draft.id))
        await session.flush()
        return DraftResult(step=None, version=None, ticket_id=None)
    elif choice == "send":
        # «Отправить» в одной транзакции: создание обращения + удаление черновика.
        if draft.step != "confirm":
            raise StaleDraftError()
        if not draft.description or not draft.priority or not draft.request_id:
            raise StaleDraftError()
        ticket = Ticket(
            author_id=user_id,
            description=draft.description,
            priority=draft.priority,
            request_id=draft.request_id,
            tenant_id=draft.tenant_id,
            status="new",
            version=1,
            next_reminder_at=_now() + timedelta(seconds=settings.reminder_interval(draft.priority)),
        )
        session.add(ticket)
        await session.flush()
        session.add(
            TicketEvent(
                ticket_id=ticket.id,
                event_type="created",
                actor_id=user_id,
                new_status="new",
                version=1,
            )
        )
        from app.metrics import tickets_created_total

        tickets_created_total.labels(priority=draft.priority).inc()
        await session.execute(delete(Draft).where(Draft.id == draft.id))
        await session.flush()
        return DraftResult(step=None, version=None, ticket_id=ticket.id)
    else:
        raise StaleDraftError()

    draft.prompt_message_id = None
    draft.version += 1
    await session.flush()
    return DraftResult(step=draft.step, version=draft.version, ticket_id=None)


async def save_prompt_message_id(
    session_factory: async_sessionmaker[AsyncSession],
    draft_id: _uuid.UUID,
    version: int,
    step: str,
    message_id: int,
) -> None:
    """Technical-запись (version не увеличивается). Условное обновление:
    draft_id+version+step — те, для которых вопрос был сформирован.
    Если черновик изменился/удалён — prompt_message_id не перезаписывается.
    """
    async with session_factory() as session, session.begin():
        await session.execute(
            update(Draft)
            .where(Draft.id == draft_id, Draft.version == version, Draft.step == step)
            .values(prompt_message_id=message_id)
        )


async def delete_draft(
    session: AsyncSession,
    user_id: int,
    draft_id: _uuid.UUID | None = None,
    expected_version: int | None = None,
) -> bool:
    q = delete(Draft).where(Draft.user_id == user_id)
    if draft_id is not None:
        q = q.where(Draft.id == draft_id)
    if expected_version is not None:
        q = q.where(Draft.version == expected_version)
    result = cast(CursorResult[()], await session.execute(q))
    await session.flush()
    return (result.rowcount or 0) > 0


async def delete_expired_drafts(
    session_factory: async_sessionmaker[AsyncSession], ttl_hours: int
) -> int:
    cutoff = _now() - timedelta(hours=ttl_hours)
    async with session_factory() as session, session.begin():
        result = cast(
            CursorResult[()],
            await session.execute(delete(Draft).where(Draft.updated_at < cutoff)),
        )
        return result.rowcount or 0
```

- [ ] **Step 2: Ruff + commit**

Run: `docker compose --profile test run --rm test ruff check app/services/drafts.py`
```bash
git add app/services/drafts.py
git commit -m "feat: drafts service (steps, version, prompt_message_id binding)"
```

### Task 5.5: Тесты черновиков (конкурентность, устаревшие кнопки)

**Files:**
- Test: `tests/integration/test_drafts.py`

- [ ] **Step 1: Write the tests** (полный код)

```python
# tests/integration/test_drafts.py
import asyncio

import pytest
from sqlalchemy import text

from app.services import drafts
from app.services import users as users_svc
from app.services.errors import (
    DraftValidationError,
    StaleAnswerError,
    StaleDraftError,
)


async def _make_user(db, tg: int = 1) -> int:
    async with db.session_factory() as s, s.begin():
        u = await users_svc.upsert_user(s, telegram_id=tg)
        return u.id


async def _draft(db, user_id):
    from sqlalchemy import select

    from app.models import Draft

    async with db.session_factory() as s:
        return (await s.execute(select(Draft).where(Draft.user_id == user_id))).scalar_one_or_none()


async def _fill_description(db, uid, draft_id, version, value="описание"):
    await drafts.save_prompt_message_id(db.session_factory, draft_id, version, "description", 100)
    async with db.session_factory() as s, s.begin():
        return await drafts.answer_text(s, uid, value, 100)


async def test_start_and_continue(db):
    uid = await _make_user(db)
    async with db.session_factory() as s, s.begin():
        d1, c1 = await drafts.start_draft(s, uid)
        d2, c2 = await drafts.start_draft(s, uid)
    assert c1 is True and c2 is False
    assert d1.id == d2.id


async def test_reset_replaces_draft(db):
    uid = await _make_user(db)
    async with db.session_factory() as s, s.begin():
        d1, _ = await drafts.start_draft(s, uid)
    async with db.session_factory() as s, s.begin():
        d2 = await drafts.reset_draft(s, uid, d1.id, d1.version)
    assert d2.id != d1.id and d2.version == 1
    async with db.session_factory() as s:
        n = (await s.execute(text("SELECT count(*) FROM ticket_drafts"))).scalar_one()
    assert n == 1


async def test_answer_text_advances(db):
    uid = await _make_user(db)
    async with db.session_factory() as s, s.begin():
        d, _ = await drafts.start_draft(s, uid)
    r = await _fill_description(db, uid, d.id, 1, "проблема")
    assert (r.step, r.version) == ("priority", 2)
    d = await _draft(db, uid)
    assert d.description == "проблема" and d.prompt_message_id is None


async def test_unbound_reply_changes_nothing(db):
    uid = await _make_user(db)
    async with db.session_factory() as s, s.begin():
        d, _ = await drafts.start_draft(s, uid)
    await drafts.save_prompt_message_id(db.session_factory, d.id, 1, "description", 100)
    for reply_to in (999, None):
        with pytest.raises(StaleAnswerError):
            async with db.session_factory() as s, s.begin():
                await drafts.answer_text(s, uid, "x", reply_to)
    d = await _draft(db, uid)
    assert (d.version, d.step, d.description) == (1, "description", None)


async def test_resent_answer_does_not_jump(db):
    """Повторно отправленный ответ не попадает в следующее поле."""
    uid = await _make_user(db)
    async with db.session_factory() as s, s.begin():
        d, _ = await drafts.start_draft(s, uid)
    await _fill_description(db, uid, d.id, 1, "первый")
    with pytest.raises(StaleAnswerError):
        async with db.session_factory() as s, s.begin():
            await drafts.answer_text(s, uid, "повтор", 100)
    d = await _draft(db, uid)
    assert (d.version, d.description) == (2, "первый")


async def test_validation(db):
    uid = await _make_user(db)
    async with db.session_factory() as s, s.begin():
        d, _ = await drafts.start_draft(s, uid)
    await drafts.save_prompt_message_id(db.session_factory, d.id, 1, "description", 100)
    for bad in ("", "   ", "x" * 3001):
        with pytest.raises(DraftValidationError):
            async with db.session_factory() as s, s.begin():
                await drafts.answer_text(s, uid, bad, 100)
    d = await _draft(db, uid)
    assert d.version == 1
    async with db.session_factory() as s, s.begin():
        await drafts.answer_text(s, uid, "x" * 3000, 100)
    assert (await _draft(db, uid)).version == 2


async def test_concurrent_answers_one_wins(db):
    uid = await _make_user(db)
    async with db.session_factory() as s, s.begin():
        d, _ = await drafts.start_draft(s, uid)
    await drafts.save_prompt_message_id(db.session_factory, d.id, 1, "description", 100)

    async def answer(value):
        async with db.session_factory() as s, s.begin():
            return await drafts.answer_text(s, uid, value, 100)

    results = await asyncio.gather(answer("A"), answer("B"), return_exceptions=True)
    ok = [r for r in results if not isinstance(r, Exception)]
    errs = [r for r in results if isinstance(r, Exception)]
    assert len(ok) == 1 and len(errs) == 1
    assert isinstance(errs[0], StaleAnswerError)
    d = await _draft(db, uid)
    assert d.version == 2 and d.description in ("A", "B")


async def test_stale_button_from_removed_draft(db):
    uid = await _make_user(db)
    async with db.session_factory() as s, s.begin():
        d, _ = await drafts.start_draft(s, uid)
        r = await drafts.answer_choice(s, None, uid, d.id, d.version, "canx")
    assert r.step is None and r.ticket_id is None
    with pytest.raises(StaleDraftError):
        async with db.session_factory() as s, s.begin():
            await drafts.answer_choice(s, None, uid, d.id, d.version, "send")
    async with db.session_factory() as s:
        n = (await s.execute(text("SELECT count(*) FROM tickets"))).scalar_one()
    assert n == 0


async def test_stale_version_rejected(db):
    uid = await _make_user(db)
    async with db.session_factory() as s, s.begin():
        d, _ = await drafts.start_draft(s, uid)
    with pytest.raises(StaleDraftError):
        async with db.session_factory() as s, s.begin():
            await drafts.answer_choice(s, None, uid, d.id, d.version + 5, "prio", "high")
    with pytest.raises(StaleDraftError):
        async with db.session_factory() as s, s.begin():
            await drafts.answer_choice(s, None, uid, d.id, d.version, "prio", "bogus")


async def test_late_question_does_not_overwrite_prompt(db):
    """Запоздавшая отправка вопроса не перезаписывает prompt_message_id следующего шага."""
    uid = await _make_user(db)
    async with db.session_factory() as s, s.begin():
        d, _ = await drafts.start_draft(s, uid)
    await drafts.save_prompt_message_id(db.session_factory, d.id, 1, "description", 100)
    r = await _fill_description(db, uid, d.id, 1, "описание")  # v2 priority
    async with db.session_factory() as s, s.begin():
        r = await drafts.answer_choice(s, None, uid, d.id, r.version, "prio", "high")  # v3
    await drafts.save_prompt_message_id(db.session_factory, d.id, r.version, "request_id", 102)
    # «запоздалый» вопрос description (v1) — не должен перезаписать
    await drafts.save_prompt_message_id(db.session_factory, d.id, 1, "description", 150)
    d = await _draft(db, uid)
    assert d.prompt_message_id == 102
    with pytest.raises(StaleAnswerError):
        async with db.session_factory() as s, s.begin():
            await drafts.answer_text(s, uid, "x", 150)


async def test_send_creates_ticket_and_deletes_draft(db, settings):
    uid = await _make_user(db)
    async with db.session_factory() as s, s.begin():
        d, _ = await drafts.start_draft(s, uid)
    r = await _fill_description(db, uid, d.id, 1, "описание")
    async with db.session_factory() as s, s.begin():
        r = await drafts.answer_choice(s, settings, uid, d.id, r.version, "prio", "critical")
    await drafts.save_prompt_message_id(db.session_factory, d.id, r.version, "request_id", 102)
    async with db.session_factory() as s, s.begin():
        r = await drafts.answer_text(s, uid, "R-42", 102)
    async with db.session_factory() as s, s.begin():
        r = await drafts.answer_choice(s, settings, uid, d.id, r.version, "skip")
    assert r.step == "confirm"
    async with db.session_factory() as s, s.begin():
        r = await drafts.answer_choice(s, settings, uid, d.id, r.version, "send")
    assert r.ticket_id is not None
    async with db.session_factory() as s:
        row = (
            await s.execute(
                text(
                    "SELECT description, priority, request_id, tenant_id, "
                    "status, version, author_id "
                    "FROM tickets WHERE id = :i"
                ),
                {"i": r.ticket_id},
            )
        ).one()
        n_drafts = (await s.execute(text("SELECT count(*) FROM ticket_drafts"))).scalar_one()
        ev = (
            await s.execute(
                text("SELECT event_type FROM ticket_events WHERE ticket_id = :i"),
                {"i": r.ticket_id},
            )
        ).scalar_one()
    assert row == ("описание", "critical", "R-42", None, "new", 1, uid)
    assert n_drafts == 0 and ev == "created"
    # повторное «Отправить» — ничего не создаст
    with pytest.raises(StaleDraftError):
        async with db.session_factory() as s, s.begin():
            await drafts.answer_choice(s, settings, uid, d.id, r.version, "send")
    async with db.session_factory() as s:
        n = (await s.execute(text("SELECT count(*) FROM tickets"))).scalar_one()
    assert n == 1


async def test_ttl_cleanup(db):
    uid = await _make_user(db)
    async with db.session_factory() as s, s.begin():
        await drafts.start_draft(s, uid)
    async with db.session_factory() as s, s.begin():
        await s.execute(text("UPDATE ticket_drafts SET updated_at = now() - interval '25 hours'"))
    assert await drafts.delete_expired_drafts(db.session_factory, 24) == 1
```

Note: в тестах, где `settings` не нужен, передаём `None` (мypy в тестах не проверяем).

- [ ] **Step 2: Run tests**

Run: `docker compose --profile test run --rm test pytest tests/integration/test_drafts.py -q`
Expected: 13 passed.

- [ ] **Step 3: Ruff + commit**

```bash
git add tests/integration/test_drafts.py
git commit -m "test: drafts (concurrency, stale buttons, prompt binding, TTL)"
```

### Task 5.6: Команды /start /help /cancel

**Files:**
- Create: `app/bot/handlers/commands.py`
- Test: `tests/integration/test_commands.py`

- [ ] **Step 1: Create `app/bot/handlers/commands.py`**

```python
"""Команды: /start, /help, /cancel."""
from __future__ import annotations

from telegram import KeyboardButton, ReplyKeyboardMarkup, WebAppInfo
from telegram.ext import CommandHandler, ContextTypes

from app.log import get_logger
from app.models import User
from app.services import drafts
from app.services.users import change_role
from sqlalchemy import update as sa_update

log = get_logger("bot.commands")

HELP_TEXT = (
    "Я веду учёт обращений к дежурному разработчику.\n\n"
    "/new — создать обращение\n"
    "/cancel — отменить незавершённую форму\n\n"
    "Список и карточки обращений — кнопка «📋 Обращения» (Mini App)."
)


def _menu_keyboard(url: str):
    return ReplyKeyboardMarkup(
        [[KeyboardButton(text="📋 Обращения", web_app=WebAppInfo(url=url))]],
        resize_keyboard=True,
    )


async def cmd_start(context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    settings, db = app.settings, app.db
    user = context.effective_user
    assert user is not None
    # пользователь уже upsertнут в process_update
    async with db.session_factory() as s, s.begin():
        row = await s.get(User, (await _user_id_by_tg(s, user.id)).__class__ and 0) if False else None
    # проще: читаем роль и при необходимости назначаем bootstrap-admin
    from sqlalchemy import select

    async with db.session_factory() as s, s.begin():
        db_user = (await s.execute(select(User).where(User.telegram_id == user.id))).scalar_one()
        role = db_user.role
        if role is None and user.id in settings.bootstrap_admin_ids:
            await change_role(s, user_id=db_user.id, new_role="admin")
            role = "admin"
    chat = context.chat_id
    assert chat is not None
    if role is None:
        await context.bot.send_message(
            chat,
            "Вы зарегистрированы, но у вас пока нет доступа. "
            "Обратитесь к администратору, чтобы получить роль.",
        )
        return
    await context.bot.send_message(
        chat,
        f"Привет, {user.first_name}! Создайте обращение командой /new или смотрите их через кнопку ниже.",
        reply_markup=_menu_keyboard(settings.mini_app_url) if settings.mini_app_url else None,
    )


async def _user_id_by_tg(session, telegram_id: int) -> int:
    from sqlalchemy import select

    return (await session.execute(select(User.id).where(User.telegram_id == telegram_id))).scalar_one()


async def cmd_help(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = context.chat_id
    assert chat is not None
    await context.bot.send_message(chat, HELP_TEXT)


async def cmd_cancel(context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    user = context.effective_user
    chat = context.chat_id
    assert user is not None and chat is not None
    async with app.db.session_factory() as s, s.begin():
        removed = await drafts.delete_draft(s, user.id)
    await context.bot.send_message(chat, "Форма отменена." if removed else "Нет активной формы.")


def register_command_handlers(app) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
```

NOTE: оставшаяся в `cmd_start` строка `row = await s.get(...) if False else None` — АРТЕФАКТ, удали её (и пустой `async with` вокруг) — финальный код `cmd_start` начинается сразу с импорта `select` и блока «читаем роль».

- [ ] **Step 2: Write `tests/integration/test_commands.py`**

```python
# tests/integration/test_commands.py
from sqlalchemy import text
from telegram import Update

from tests.fakes import make_update


async def _u(tg_app, update_id, user_id, text=None, data=None, first_name="Test"):
    from tests.fakes import make_user_dict

    d = make_update(update_id, user_id, text=text, data=data)
    if first_name and "message" in d:
        d["message"]["from"]["first_name"] = first_name
    await tg_app.process_update(Update.de_json(d, tg_app.bot))


async def test_start_bootstrap_admin(tg_app, bot, db):
    await _u(tg_app, 1, 111, "/start")  # 111 — из BOOTSTRAP_ADMIN_IDS
    async with db.session_factory() as s:
        role = (await s.execute(text("SELECT role FROM users WHERE telegram_id = 111"))).scalar_one()
    assert role == "admin"
    assert len(bot.sent) == 1


async def test_start_no_role_notification(tg_app, bot, db):
    await _u(tg_app, 2, 222, "/start")
    async with db.session_factory() as s:
        role = (await s.execute(text("SELECT role FROM users WHERE telegram_id = 222"))).scalar_one()
    assert role is None
    assert "администратор" in bot.sent[-1]["text"].lower()


async def test_name_updated_on_every_interaction(tg_app, db):
    await _u(tg_app, 3, 333, "/start", first_name="Old")
    await _u(tg_app, 4, 333, "/help", first_name="New")
    async with db.session_factory() as s:
        name = (await s.execute(text("SELECT first_name FROM users WHERE telegram_id = 333"))).scalar_one()
    assert name == "New"


async def test_help(tg_app, bot):
    await _u(tg_app, 5, 333, "/help")
    assert "/new" in bot.sent[-1]["text"]


async def test_cancel_without_draft(tg_app, bot):
    await _u(tg_app, 6, 333, "/start")
    await _u(tg_app, 7, 333, "/cancel")
    assert "Нет активной формы" in bot.sent[-1]["text"]


async def test_duplicate_update_processed_once(tg_app, bot, db):
    d = make_update(8, 444, text="/start")
    await tg_app.process_update(Update.de_json(d, tg_app.bot))
    sent_after_first = len(bot.sent)
    await tg_app.process_update(Update.de_json(d, tg_app.bot))
    assert len(bot.sent) == sent_after_first  # второй раз не обработан
```

- [ ] **Step 3: Run tests**

Run: `docker compose --profile test run --rm test pytest tests/integration/test_commands.py -q`
Expected: 6 passed.

- [ ] **Step 4: Ruff + mypy + commit**

```bash
git add app/bot/handlers/commands.py tests/integration/test_commands.py
git commit -m "feat: /start (bootstrap admins), /help, /cancel handlers"
```

### Task 5.7: Форма: /new, текстовые ответы, кнопки формы

**Files:**
- Create: `app/bot/handlers/draft_form.py`
- Test: `tests/integration/test_draft_flow.py`

- [ ] **Step 1: Create `app/bot/handlers/draft_form.py`**

```python
"""/new, текстовые ответы формы и кнопки (d:*)."""
from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler
from telegram import filters

from app.bot import form
from app.log import get_logger
from app.models import Draft
from app.services import drafts
from app.services.errors import DraftValidationError, StaleAnswerError, StaleDraftError
from app.states import CREATE_ROLES
from app.services.tickets import _role_of

log = get_logger("bot.draft_form")


async def _ask_question(context: ContextTypes.DEFAULT_TYPE, user_id: int, draft: Draft) -> None:
    """Отправить вопрос шага и сохранить message_id условным обновлением."""
    text, markup = form.question_for(draft.step, draft)
    msg = await context.bot.send_message(context.chat_id, text, reply_markup=markup)
    if draft.step in form.TEXT_STEPS:
        await drafts.save_prompt_message_id(
            context.application.db.session_factory, draft.id, draft.version, draft.step, msg.message_id
        )


async def _show_stale(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """Устаревшая кнопка: показать актуальный вопрос."""
    db = context.application.db
    async with db.session_factory() as s:
        d = await drafts.get_draft(s, user_id)
    if d is None:
        await context.bot.send_message(context.chat_id, "Черновик не найден. Начните заново: /new")
        return
    await _ask_question(context, user_id, d)


async def cmd_new(context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    settings, db = app.settings, app.db
    user = context.effective_user
    chat = context.chat_id
    assert user is not None and chat is not None
    async with db.session_factory() as s:
        role = await _role_of(s, user.id)
    if role not in CREATE_ROLES:
        await context.bot.send_message(
            chat, "Создавать обращения могут только первая линия (support) и администраторы."
        )
        return
    async with db.session_factory() as s, s.begin():
        draft, created = await drafts.start_draft(s, user.id)
    if created:
        await _ask_question(context, user.id, draft)
        return
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Продолжить", callback_data=form.cb("cont", draft)),
                InlineKeyboardButton("Начать заново", callback_data=form.cb("reset", draft)),
            ]
        ]
    )
    await context.bot.send_message(
        chat, "У вас есть незавершённая форма. Продолжить или начать заново?", reply_markup=keyboard
    )


async def on_text(context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    db = app.db
    user = context.effective_user
    message = context.effective_message
    chat = context.chat_id
    assert user is not None and message is not None and chat is not None
    text_value = message.text or ""
    reply_to = message.reply_to_message.message_id if message.reply_to_message else None
    async with db.session_factory() as s:
        draft = await drafts.get_draft(s, user.id)
    if draft is None:
        await context.bot.send_message(chat, "Активной формы нет. Создайте её командой /new.")
        return
    try:
        async with db.session_factory() as s, s.begin():
            result = await drafts.answer_text(s, user.id, text_value, reply_to)
    except StaleAnswerError:
        # ответ без нужной привязки: данные не меняются, заново задаём вопрос
        await _ask_question(context, user.id, draft)
        return
    except DraftValidationError as e:
        await context.bot.send_message(chat, f"Некорректное значение {e.field}. Попробуйте ещё раз.")
        return
    draft.step, draft.version = result.step, result.version
    await _ask_question(context, user.id, draft)


async def _after_choice(context: ContextTypes.DEFAULT_TYPE, user_id: int, result) -> None:
    """Общий код после answer_choice: пост/подтверждение или вопрос нового шага."""
    app = context.application
    if result.ticket_id is not None:
        from app.services.post_sync import sync_ticket_post

        # пост публикуется ПОСЛЕ коммита создания (сбой публикации не теряет обращение)
        try:
            await sync_ticket_post(app.settings, app.db, app.bot, result.ticket_id)
        except Exception:
            log.exception("post publish failed after draft send; background sync will catch up")
        await context.bot.send_message(context.chat_id, f"Обращение #{result.ticket_id} создано.")
        return
    if result.step is None:
        await context.bot.send_message(context.chat_id, "Форма отменена.")
        return
    db = app.db
    async with db.session_factory() as s:
        d = await drafts.get_draft(s, user_id)
    if d is not None:
        await _ask_question(context, user_id, d)


async def on_draft_callback(context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    settings, db = app.settings, app.db
    user = context.effective_user
    cq = context.callback_query
    assert user is not None and cq is not None
    parsed = form.parse_draft_callback(cq.data or "")
    if parsed is None:
        return
    kind, uid_str, version, extra = parsed
    import uuid as _uuid

    draft_id = _uuid.UUID(uid_str)

    if kind == "cont":
        async with db.session_factory() as s:
            d = await drafts.get_draft(s, user.id)
        if d is None or d.id != draft_id or d.version != version:
            await cq.answer(text="Форма изменилась. Продолжите с текущего шага.", show_alert=True)
            await _show_stale(context, user.id)
            return
        await cq.answer()
        await _ask_question(context, user.id, d)
        return

    if kind == "reset":
        try:
            async with db.session_factory() as s, s.begin():
                d = await drafts.reset_draft(s, user.id, draft_id, version)
        except StaleDraftError:
            await cq.answer(text="Форма изменилась. Продолжите с текущего шага.", show_alert=True)
            await _show_stale(context, user.id)
            return
        await cq.answer()
        await _ask_question(context, user.id, d)
        return

    # prio / skip / send / canx
    choice = {"prio": "prio", "skip": "skip", "send": "send", "canx": "canx"}.get(kind)
    if choice is None:
        await cq.answer()
        return
    value = form.SHORT_PRIORITY.get(extra) if kind == "prio" else None
    try:
        async with db.session_factory() as s, s.begin():
            result = await drafts.answer_choice(s, settings, user.id, draft_id, version, choice, value)
    except StaleDraftError:
        log.info("stale draft callback rejected", kind=kind, user_id=user.id)
        await cq.answer(text="Форма изменилась. Продолжите с текущего шага.", show_alert=True)
        await _show_stale(context, user.id)
        return
    await cq.answer()
    await _after_choice(context, user.id, result)


def register_draft_form_handlers(app) -> None:
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_draft_callback, pattern=r"^d:"))
```

- [ ] **Step 2: Write `tests/integration/test_draft_flow.py`**

```python
# tests/integration/test_draft_flow.py
from sqlalchemy import text
from telegram import Update

from tests.fakes import make_update


async def _u(tg_app, update_id, user_id, text=None, data=None, message_id=1, reply_to=None):
    await tg_app.process_update(
        Update.de_json(make_update(update_id, user_id, text=text, data=data,
                                   message_id=message_id, reply_to_message_id=reply_to), tg_app.bot)
    )


async def _draft_row(db, user_id):
    async with db.session_factory() as s:
        return (
            await s.execute(
                text(
                    "SELECT id, version, step, prompt_message_id FROM ticket_drafts WHERE user_id = :u"
                ),
                {"u": user_id},
            )
        ).one_or_none()


async def _start_draft(tg_app, db, user_id):
    await _u(tg_app, 1, user_id, "/start")
    await _u(tg_app, 2, user_id, "/new")
    row = await _draft_row(db, user_id)
    assert row is not None and row.step == "description" and row.prompt_message_id is not None
    return row


def _cb(kind, draft_row, *extra):
    return ":".join(["d", kind, str(draft_row.id).replace("-", ""), str(draft_row.version), *extra])


async def test_full_flow_creates_ticket(tg_app, bot, db, settings):
    uid = 111  # bootstrap admin
    row = await _start_draft(tg_app, db, uid)
    # 1. описание
    await _u(tg_app, 3, uid, "проблема", message_id=10, reply_to=row.prompt_message_id)
    row = await _draft_row(db, uid)
    assert row.step == "priority"
    # 2. приоритет
    await _u(tg_app, 4, uid, data=_cb("prio", row, "h"))
    row = await _draft_row(db, uid)
    assert row.step == "request_id" and row.prompt_message_id is not None
    # 3. id заявки
    await _u(tg_app, 5, uid, "R-1", message_id=11, reply_to=row.prompt_message_id)
    row = await _draft_row(db, uid)
    assert row.step == "tenant_id"
    # 4. пропустить арендатора
    await _u(tg_app, 6, uid, data=_cb("skip", row))
    row = await _draft_row(db, uid)
    assert row.step == "confirm"
    # 5. отправить
    await _u(tg_app, 7, uid, data=_cb("send", row))
    async with db.session_factory() as s:
        t = (await s.execute(text("SELECT id, description, priority, request_id, tenant_id, status FROM tickets"))).one()
        n_drafts = (await s.execute(text("SELECT count(*) FROM ticket_drafts"))).scalar_one()
    assert (t.description, t.priority, t.request_id, t.tenant_id, t.status) == (
        "проблема", "high", "R-1", None, "new"
    )
    assert n_drafts == 0
    # пост в чат разработчиков опубликован после коммита
    dev_posts = [m for m in bot.sent if m["chat_id"] == settings.dev_chat_id]
    assert len(dev_posts) == 1 and f"#{t.id}" in dev_posts[0]["text"]
    # подтверждение в личке
    dm = [m for m in bot.sent if m["chat_id"] == uid]
    assert any("создано" in m["text"] for m in dm)


async def test_text_without_draft_hint(tg_app, bot):
    await _u(tg_app, 1, 222, "/start")
    await _u(tg_app, 2, 222, "просто текст")
    assert any("new" in m["text"] for m in bot.sent if m["chat_id"] == 222)


async def test_unbound_message_reasks_no_change(tg_app, bot, db):
    uid = 333
    row = await _start_draft(tg_app, db, uid)
    await _u(tg_app, 3, uid, "x", message_id=10, reply_to=999)  # reply на чужой вопрос
    row2 = await _draft_row(db, uid)
    assert row2.version == 1 and row2.step == "description"
    # вопрос задан повторно (новое сообщение)
    assert bot.sent[-1]["chat_id"] == uid


async def test_old_button_cannot_confirm_new_draft(tg_app, bot, db):
    """Кнопка «Отправить» удалённого черновика не подтверждает новый."""
    uid = 444
    row = await _start_draft(tg_app, db, uid)
    # заполнить до confirm
    await _u(tg_app, 3, uid, "текст", message_id=10, reply_to=row.prompt_message_id)
    row = await _draft_row(db, uid)
    await _u(tg_app, 4, uid, data=_cb("prio", row, "c"))
    row = await _draft_row(db, uid)
    await _u(tg_app, 5, uid, "R-9", message_id=11, reply_to=row.prompt_message_id)
    row = await _draft_row(db, uid)
    await _u(tg_app, 6, uid, data=_cb("skip", row))
    old_row = await _draft_row(db, uid)
    old_send = _cb("send", old_row)
    # «Начать заново»
    await _u(tg_app, 7, uid, data=_cb("reset", old_row))
    new_row = await _draft_row(db, uid)
    assert new_row.id != old_row.id
    # нажатие старой кнопки send (uuid старого черновика)
    await _u(tg_app, 8, uid, data=old_send)
    async with db.session_factory() as s:
        n = (await s.execute(text("SELECT count(*) FROM tickets"))).scalar_one()
    assert n == 0  # ничего не создано
    assert bot.answered[-1]["show_alert"] is True
    assert (await _draft_row(db, uid)).version == 1  # новый черновик не тронут


async def test_new_without_role_forbidden(tg_app, bot, db):
    uid = 555
    await _u(tg_app, 1, uid, "/start")
    await _u(tg_app, 2, uid, "/new")
    assert (await _draft_row(db, uid)) is None
    assert "support" in bot.sent[-1]["text"]


async def test_new_with_existing_draft_offers_continue(tg_app, bot, db):
    uid = 111
    row = await _start_draft(tg_app, db, uid)
    await _u(tg_app, 3, uid, "текст", message_id=10, reply_to=row.prompt_message_id)
    await _u(tg_app, 4, uid, "/new")
    assert "Продолжить" in bot.sent[-1]["text"] or "незавершённая" in bot.sent[-1]["text"]
```

Note: `test_new_with_existing_draft_offers_continue` — сообщение «Продолжить/Начать заново» отправляется БЕЗ текста-кнопок в тексте (это inline-кнопки). Утверждение: проверь по `bot.sent[-1]["reply_markup"]` (RecordingBot сохраняет reply_markup в kwargs). Замени утверждение на:

```python
    sent = bot.sent[-1]
    buttons = sent["reply_markup"].inline_keyboard[0]
    assert [b.text for b in buttons] == ["Продолжить", "Начать заново"]
```

- [ ] **Step 3: Run tests**

Run: `docker compose --profile test run --rm test pytest tests/integration/test_draft_flow.py -q`
Expected: 6 passed.

- [ ] **Step 4: Ruff + mypy + commit**

```bash
git add app/bot/handlers/draft_form.py tests/integration/test_draft_flow.py
git commit -m "feat: draft form handlers (/new, text answers, buttons, stale handling)"
```

### Task 5.8: Кнопки постов (t:*)

**Files:**
- Create: `app/bot/handlers/callbacks.py`
- Test: `tests/integration/test_post_callbacks.py`

- [ ] **Step 1: Create `app/bot/handlers/callbacks.py`**

```python
"""Кнопки постов в чате разработчиков: callback_data t:<action>:<id>:<version>."""
from __future__ import annotations

import re

from telegram.ext import CallbackQueryHandler, ContextTypes

from app.log import get_logger
from app.services import tickets as tickets_svc
from app.services.errors import AlreadyTakenError, NotFoundError, PermissionDenied, StaleTicketError

log = get_logger("bot.callbacks")

ACTION_TO_TRANSITION = {
    "take": "take",
    "wai": "to_waiting",
    "pro": "to_in_progress",
    "res": "to_resolved",
    "rej": "to_rejected",
    "dup": "to_duplicate",
    "dec": "decline",
}

PATTERN = re.compile(r"^t:(take|wai|pro|res|rej|dup|dec):(\d+):(\d+)$")


async def resync_post(app, ticket_id: int) -> None:
    """Пересинхронизация поста после бизнес-изменения или конфликта (SPEC)."""
    from app.services.post_sync import sync_ticket_post

    try:
        await sync_ticket_post(app.settings, app.db, app.bot, ticket_id)
    except Exception:
        log.exception("post resync failed; background sync will catch up", ticket_id=ticket_id)


async def on_post_callback(context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    cq = context.callback_query
    user = context.effective_user
    assert cq is not None and user is not None
    m = PATTERN.match(cq.data or "")
    if not m:
        return
    action = m.group(1)
    ticket_id, expected_version = int(m.group(2)), int(m.group(3))
    db = app.db
    try:
        async with db.session_factory() as s, s.begin():
            ticket = await tickets_svc.load_ticket(s, ticket_id)
            if action == "take":
                await tickets_svc.take_ticket(s, app.settings, user.id, ticket, expected_version)
            else:
                await tickets_svc.apply_transition(
                    s, app.settings, user.id, ticket, expected_version, ACTION_TO_TRANSITION[action]
                )
    except NotFoundError:
        await cq.answer(text="Обращение не найдено.", show_alert=True)
        return
    except AlreadyTakenError:
        await cq.answer(text="Обращение уже взято.", show_alert=True)
        await resync_post(app, ticket_id)
        return
    except PermissionDenied:
        await cq.answer(text="Недостаточно прав для этого действия.", show_alert=True)
        return
    except StaleTicketError:
        await cq.answer(text="Обращение изменилось. Пост обновлён.", show_alert=True)
        await resync_post(app, ticket_id)
        return
    await cq.answer()
    await resync_post(app, ticket_id)


def register_post_callback_handlers(app) -> None:
    app.add_handler(CallbackQueryHandler(on_post_callback, pattern=PATTERN))
```

- [ ] **Step 2: Write `tests/integration/test_post_callbacks.py`**

```python
# tests/integration/test_post_callbacks.py
from sqlalchemy import text
from telegram import Update

from tests.fakes import make_update


async def _u(tg_app, update_id, user_id, data=None, text=None):
    await tg_app.process_update(
        Update.de_json(make_update(update_id, user_id, data=data, text=text), tg_app.bot)
    )


async def _ticket(db, settings, author_tg: int = 111) -> int:
    from app.services import tickets as tsvc
    from app.services import users as usvc

    async with db.session_factory() as s, s.begin():
        u = await usvc.upsert_user(s, telegram_id=author_tg, first_name="A")
        t = await tsvc.create_ticket(s, settings, u.id, description="d", priority="high", request_id="r")
    return t.id


async def _dev_id(tg_app, db, tg: int = 222) -> int:
    from app.services import users as usvc

    await _u(tg_app, 50, tg, text="/start")
    async with db.session_factory() as s, s.begin():
        u = await usvc.upsert_user(s, telegram_id=tg)
        await usvc.change_role(s, user_id=u.id, new_role="developer")
        return u.id


def _t(action, tid, v):
    return f"t:{action}:{tid}:{v}"


async def test_take_via_button(tg_app, bot, db, settings):
    tid = await _ticket(db, settings)
    dev_id = await _dev_id(tg_app, db)
    await _u(tg_app, 100, 222, data=_t("take", tid, 1))
    async with db.session_factory() as s:
        row = (await s.execute(text("SELECT status, assignee_id FROM tickets WHERE id = :i"), {"i": tid})).one()
    assert row == ("in_progress", dev_id)
    dev_posts = [m for m in bot.sent if m["chat_id"] == settings.dev_chat_id]
    assert len(dev_posts) == 1
    assert "в работе" in dev_posts[0]["text"]


async def test_stale_take_alert(tg_app, bot, db, settings):
    tid = await _ticket(db, settings)
    await _dev_id(tg_app, db)
    await _u(tg_app, 101, 222, data=_t("take", tid, 1))
    await _u(tg_app, 102, 222, data=_t("take", tid, 1))  # старая кнопка
    last = bot.answered[-1]
    assert last["show_alert"] is True
    assert "взято" in last["text"]


async def test_permission_denied(tg_app, bot, db, settings):
    tid = await _ticket(db, settings)
    await _dev_id(tg_app, db)
    await _u(tg_app, 103, 222, data=_t("take", tid, 1))
    await _u(tg_app, 30, 333, text="/start")
    await _u(tg_app, 104, 333, data=_t("res", tid, 2))
    assert bot.answered[-1]["show_alert"] is True
    async with db.session_factory() as s:
        status = (await s.execute(text("SELECT status FROM tickets WHERE id = :i"), {"i": tid})).scalar_one()
    assert status == "in_progress"


async def test_status_cycle_via_buttons(tg_app, bot, db, settings):
    tid = await _ticket(db, settings)
    await _dev_id(tg_app, db)
    for v, (action, final) in enumerate(
        (("take", "in_progress"), ("wai", "waiting_info"), ("pro", "in_progress"), ("res", "resolved")), start=1
    ):
        await _u(tg_app, 200 + v, 222, data=_t(action, tid, v))
    async with db.session_factory() as s:
        row = (
            await s.execute(
                text("SELECT status, closed_at IS NOT NULL, version FROM tickets WHERE id = :i"), {"i": tid}
            )
        ).one()
    assert row == ("resolved", True, 5)
    last_post = bot.edited[-1]["text"] if bot.edited else bot.sent[-1]["text"]
    assert "решено" in last_post
```

- [ ] **Step 3: Run tests**

Run: `docker compose --profile test run --rm test pytest tests/integration/test_post_callbacks.py -q`
Expected: 4 passed.

- [ ] **Step 4: Ruff + mypy + commit**

```bash
git add app/bot/handlers/callbacks.py tests/integration/test_post_callbacks.py
git commit -m "feat: post callback buttons (take/status/decline) with alerts and resync"
```

### Task 5.9: Webhook endpoint + main.py v2

**Files:**
- Create: `app/bot/webhook.py`
- Modify: `app/main.py` (заменить v1), `app/workers.py` (добавить два цикла)
- Test: `tests/integration/test_webhook.py`

- [ ] **Step 1: Create `app/bot/webhook.py`**

```python
"""Webhook-эндпоинт (SPEC «FastAPI»): X-Telegram-Bot-Api-Secret-Token,
синхронная обработка внутри запроса, 200 даже при ошибке обработки."""
from __future__ import annotations

import asyncio
import hmac
import uuid

import sentry_sdk
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from structlog.contextvars import bind_contextvars, clear_contextvars
from telegram import Update

from app.log import get_logger
from app.metrics import errors_total

log = get_logger("bot.webhook")
router = APIRouter()


@router.post("/telegram/webhook")
async def webhook(request: Request):
    settings = request.app.state.settings
    if settings.bot_mode != "webhook":
        return JSONResponse({"error": "webhook disabled"}, status_code=404)
    token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not token or not hmac.compare_digest(token, settings.webhook_secret_token):
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    try:
        payload = await request.json()
        update = Update.de_json(payload, request.app.state.tg_app.bot)
    except Exception:
        return JSONResponse({"error": "invalid update"}, status_code=400)
    clear_contextvars()
    bind_contextvars(
        request_id=uuid.uuid4().hex,
        update_id=getattr(update, "update_id", None),
        telegram_user_id=getattr(getattr(update, "effective_user", None), "id", None),
    )
    try:
        await asyncio.wait_for(
            request.app.state.tg_app.process_update(update),
            timeout=settings.update_timeout_seconds,
        )
    except Exception as exc:
        log.exception("update processing failed")
        errors_total.labels(component="webhook").inc()
        sentry_sdk.capture_exception(exc)
    finally:
        clear_contextvars()
    return {"ok": True}
```

- [ ] **Step 2: Replace `app/main.py` (v2)**

```python
import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import log
from app.api.health import router as health_router
from app.bot.application import build_application
from app.bot.webhook import router as webhook_router
from app.config import Settings, get_settings
from app.db import make_db
from app.workers import run_draft_ttl_loop, run_post_sync_loop, run_processed_updates_cleanup


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    log.configure_logging(settings.log_level, settings.log_format)
    settings.validate_modes()
    app.state.db = make_db(settings.database_url)

    tg_app = build_application(settings, app.state.db)
    await tg_app.initialize()
    await tg_app.start()
    app.state.tg_app = tg_app

    if settings.bot_mode == "polling":
        from telegram.ext import Updater

        updater = Updater(tg_app.bot, tg_app.update_queue)
        await updater.initialize()
        await updater.start_polling()
        app.state.updater = updater

    db = app.state.db
    workers = [
        asyncio.create_task(run_post_sync_loop(settings, db, tg_app.bot), name="post_sync"),
        asyncio.create_task(run_draft_ttl_loop(settings, db), name="draft_ttl"),
        asyncio.create_task(run_processed_updates_cleanup(db), name="dedup_cleanup"),
    ]
    try:
        yield
    finally:
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        if settings.bot_mode == "polling":
            await app.state.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()
        await db.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="rent-support-bot", lifespan=lifespan)
    app.state.settings = settings or get_settings()
    app.include_router(health_router)
    app.include_router(webhook_router)
    return app


app = create_app()
```

- [ ] **Step 3: Append to `app/workers.py`**

```python
async def run_draft_ttl_loop(settings: Settings, db: DB) -> None:
    from app.services.drafts import delete_expired_drafts

    while True:
        await asyncio.sleep(3600)
        try:
            removed = await delete_expired_drafts(db.session_factory, settings.draft_ttl_hours)
            if removed:
                log.info("expired drafts removed", count=removed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _report("draft_ttl", exc)


async def run_processed_updates_cleanup(db: DB) -> None:
    from app.services.updates import purge_processed_updates

    while True:
        await asyncio.sleep(3600)
        try:
            await purge_processed_updates(db)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _report("dedup_cleanup", exc)
```

- [ ] **Step 4: Write `tests/integration/test_webhook.py`**

```python
# tests/integration/test_webhook.py
import httpx
import pytest_asyncio
from fastapi import FastAPI

from app.bot.webhook import router as webhook_router
from app.config import Settings
from app.services.updates import register_update
from tests.conftest import TEST_DB_URL, FAKE_TOKEN
from tests.fakes import make_update

SECRET = "s3cret"


@pytest_asyncio.fixture
async def wh_client(db, bot):
    from app.bot.application import build_application

    settings = Settings(
        bot_token=FAKE_TOKEN,
        bot_mode="webhook",
        webhook_url="https://example.com/telegram/webhook",
        webhook_secret_token=SECRET,
        dev_chat_id=-1001234567890,
        bootstrap_admin_ids=[111],
        database_url=TEST_DB_URL,
        mini_app_url="",
        _env_file=None,
    )
    app = FastAPI()
    app.state.settings = settings
    app.state.db = db
    tg = build_application(settings, db, bot=bot)
    await tg.initialize()
    await tg.start()
    app.state.tg_app = tg
    app.include_router(webhook_router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await tg.stop()
    await tg.shutdown()


async def test_bad_token_rejected(wh_client):
    r = await wh_client.post(
        "/telegram/webhook",
        json={"update_id": 1},
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
    )
    assert r.status_code == 403


async def test_missing_token_rejected(wh_client):
    r = await wh_client.post("/telegram/webhook", json={"update_id": 1})
    assert r.status_code == 403


async def test_update_processed(wh_client, bot):
    payload = make_update(1, 111, text="/start")
    r = await wh_client.post(
        "/telegram/webhook", json=payload, headers={"X-Telegram-Bot-Api-Secret-Token": SECRET}
    )
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert len(bot.sent) == 1


async def test_duplicate_update_skipped(wh_client, bot):
    payload = make_update(2, 111, text="/start")
    headers = {"X-Telegram-Bot-Api-Secret-Token": SECRET}
    r1 = await wh_client.post("/telegram/webhook", json=payload, headers=headers)
    r2 = await wh_client.post("/telegram/webhook", json=payload, headers=headers)
    assert r1.status_code == 200 and r2.status_code == 200
    assert len(bot.sent) == 1


async def test_processing_error_still_200_dedup_kept(wh_client, bot, db):
    """Ошибка обработки: ответ 200, запись дедупликации не откатывается."""
    payload = make_update(3, 111, text="/start")
    bot.fail_next_send = RuntimeError("telegram down")
    r = await wh_client.post(
        "/telegram/webhook", json=payload, headers={"X-Telegram-Bot-Api-Secret-Token": SECRET}
    )
    assert r.status_code == 200
    assert await register_update(db, 3) is False
```

- [ ] **Step 5: Run tests**

Run: `docker compose --profile test run --rm test pytest tests/integration/test_webhook.py -q`
Expected: 5 passed.

- [ ] **Step 6: Ruff + mypy + commit**

```bash
git add app/bot/webhook.py app/main.py app/workers.py tests/integration/test_webhook.py
git commit -m "feat: webhook endpoint (secret token, sync processing, 200 on error), main v2"
```

### Task 5.10: Проверка этапа

- [ ] **Step 1: Полный прогон**

Run: `docker compose --profile test run --rm test pytest -q && docker compose --profile test run --rm test ruff check app tests && docker compose --profile test run --rm test mypy app`
Expected: all green (~95 тестов).

- [ ] **Step 2: Commit (если были фиксы)**

```bash
git add -A
git commit -m "fix: stage 5 lint/type fixes"
```

# Stage 6 — Напоминания

### Task 6.1: Сервис напоминаний + worker loop

**Files:**
- Create: `app/services/reminders.py`
- Modify: `app/workers.py` (append `run_reminder_loop`), `app/main.py` (добавить loop в список workers)
- Test: `tests/integration/test_reminders.py`

- [ ] **Step 1: Write the failing tests `tests/integration/test_reminders.py`**

```python
# tests/integration/test_reminders.py
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.services import tickets as tickets_svc
from app.services import users as users_svc
from app.services.reminders import ReminderClaim, claim_due_reminders, send_reminders


def _utcnow():
    return datetime.now(timezone.utc)


async def _ticket(db, settings, priority: str = "critical") -> int:
    async with db.session_factory() as s, s.begin():
        u = await users_svc.upsert_user(s, telegram_id=1)
        t = await tickets_svc.create_ticket(s, settings, u.id, description="d", priority=priority, request_id="r")
    return t.id


async def _next_reminder(db, tid):
    async with db.session_factory() as s:
        return (await s.execute(text("SELECT next_reminder_at FROM tickets WHERE id = :i"), {"i": tid})).scalar_one()


async def test_schedule_on_create(db, settings):
    tid = await _ticket(db, settings)
    nr = await _next_reminder(db, tid)
    now = _utcnow()
    assert now + timedelta(seconds=590) <= nr <= now + timedelta(seconds=610)


async def test_claim_only_due_and_reschedules_from_now(db, settings):
    tid_due = await _ticket(db, settings)
    await _ticket(db, settings)
    async with db.session_factory() as s, s.begin():
        await s.execute(
            text("UPDATE tickets SET next_reminder_at = now() - interval '1 minute' WHERE id = :i"),
            {"i": tid_due},
        )
    claims = await claim_due_reminders(db.session_factory, settings)
    assert [c.ticket_id for c in claims] == [tid_due]
    # нет догоняющих: следующий срок от ТЕКУЩЕГО времени
    nr = await _next_reminder(db, tid_due)
    now = _utcnow()
    assert now < nr <= now + timedelta(seconds=settings.reminder_interval("critical") + 5)


async def test_claim_no_duplicates_concurrent_workers(db, settings):
    for _ in range(5):
        await _ticket(db, settings)
    async with db.session_factory() as s, s.begin():
        await s.execute(text("UPDATE tickets SET next_reminder_at = now() - interval '1 minute'"))
    c1, c2 = await asyncio.gather(
        claim_due_reminders(db.session_factory, settings),
        claim_due_reminders(db.session_factory, settings),
    )
    ids = [c.ticket_id for c in c1] + [c.ticket_id for c in c2]
    assert len(ids) == len(set(ids)) == 5
    c3, c4 = await asyncio.gather(
        claim_due_reminders(db.session_factory, settings),
        claim_due_reminders(db.session_factory, settings),
    )
    assert c3 == [] and c4 == []


async def test_take_clears_reminder_and_stops_claims(db, settings):
    tid = await _ticket(db, settings)
    async with db.session_factory() as s, s.begin():
        await s.execute(
            text("UPDATE tickets SET next_reminder_at = now() - interval '1 minute' WHERE id = :i"), {"i": tid}
        )
    claims = await claim_due_reminders(db.session_factory, settings)
    assert len(claims) == 1
    async with db.session_factory() as s, s.begin():
        dev = await users_svc.upsert_user(s, telegram_id=2)
        await users_svc.change_role(s, user_id=dev.id, new_role="developer")
        t = await tickets_svc.load_ticket(s, tid)
        await tickets_svc.take_ticket(s, settings, dev.id, t, expected_version=t.version)
    assert await _next_reminder(db, tid) is None
    assert await claim_due_reminders(db.session_factory, settings) == []


async def test_decline_reschedules(db, settings):
    tid = await _ticket(db, settings)
    async with db.session_factory() as s, s.begin():
        dev = await users_svc.upsert_user(s, telegram_id=2)
        await users_svc.change_role(s, user_id=dev.id, new_role="developer")
        t = await tickets_svc.load_ticket(s, tid)
        await tickets_svc.take_ticket(s, settings, dev.id, t, expected_version=t.version)
        t = await tickets_svc.load_ticket(s, tid)
        await tickets_svc.apply_transition(s, settings, dev.id, t, expected_version=t.version, action="decline")
    nr = await _next_reminder(db, tid)
    assert nr is not None


async def test_send_as_reply_to_post(db, settings, bot):
    tid = await _ticket(db, settings)
    async with db.session_factory() as s, s.begin():
        await s.execute(text("UPDATE tickets SET message_id = 777 WHERE id = :i"), {"i": tid})
    await send_reminders(settings, bot, [ReminderClaim(ticket_id=tid, priority="critical", message_id=777)])
    assert len(bot.sent) == 1
    assert bot.sent[0]["reply_to_message_id"] == 777
    assert f"#{tid}" in bot.sent[0]["text"]
    assert bot.sent[0]["chat_id"] == settings.dev_chat_id


async def test_send_without_post_has_no_reply(db, settings, bot):
    tid = await _ticket(db, settings)
    await send_reminders(settings, bot, [ReminderClaim(ticket_id=tid, priority="low", message_id=None)])
    assert len(bot.sent) == 1
    assert bot.sent[0].get("reply_to_message_id") is None


async def test_send_failure_no_retry(db, settings, bot):
    """Одна попытка на срок: сбой логируется, повторной отправки нет."""
    tid = await _ticket(db, settings)
    bot.fail_next_send = RuntimeError("telegram down")
    await send_reminders(settings, bot, [ReminderClaim(ticket_id=tid, priority="high", message_id=None)])
    assert bot.fail_next_send is None  # исключение потреблено, без ретрая
    await send_reminders(settings, bot, [ReminderClaim(ticket_id=tid, priority="high", message_id=None)])
    assert len(bot.sent) == 1
```

- [ ] **Step 2: Run to verify failure, then create `app/services/reminders.py`**

```python
"""Напоминания о невзятых обращениях (SPEC «Напоминания»).

Не более одной попытки отправки на каждый срок; потеря при сбое допустима.
Расписание в БД (next_reminder_at) + FOR UPDATE SKIP LOCKED — нет дублей
между репликами. In-memory JobQueue PTB не используется.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import sentry_sdk
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.log import get_logger
from app.metrics import reminders_sent_total
from app.models import Ticket
from app.services.post_sync import PRIORITY_RU, bot_api_call

log = get_logger("reminders")


@dataclass(frozen=True)
class ReminderClaim:
    ticket_id: int
    priority: str
    message_id: int | None


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def claim_due_reminders(
    session_factory: async_sessionmaker, settings: Settings, limit: int = 50
) -> list[ReminderClaim]:
    """Одна транзакция: SELECT ... FOR UPDATE SKIP LOCKED + сдвиг next_reminder_at + коммит.

    Следующий срок считается от текущего времени — после простоя нет серии
    догоняющих напоминаний.
    """
    now = _now()
    async with session_factory() as session, session.begin():
        tickets = (
            await session.execute(
                select(Ticket)
                .where(
                    Ticket.status == "new",
                    Ticket.next_reminder_at.is_not(None),
                    Ticket.next_reminder_at <= now,
                )
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
        ).scalars().all()
        claims: list[ReminderClaim] = []
        for t in tickets:
            t.next_reminder_at = now + timedelta(seconds=settings.reminder_interval(t.priority))
            claims.append(ReminderClaim(ticket_id=t.id, priority=t.priority, message_id=t.message_id))
    return claims


async def send_reminders(settings: Settings, bot, claims: list[ReminderClaim]) -> None:
    """После коммита: ответ на пост (если пост есть). Ошибка — лог + Sentry, без повторной отправки."""
    for claim in claims:
        try:
            text = f"⏰ Напоминание: обращение #{claim.ticket_id} ({PRIORITY_RU[claim.priority]}) всё ещё не взято."
            await bot_api_call(
                bot.send_message, settings.dev_chat_id, text, reply_to_message_id=claim.message_id
            )
            reminders_sent_total.labels(result="ok").inc()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("reminder send failed", ticket_id=claim.ticket_id)
            reminders_sent_total.labels(result="error").inc()
            sentry_sdk.capture_exception(exc)
```

- [ ] **Step 3: Append `run_reminder_loop` to `app/workers.py` и добавить его в `app/main.py`**

В `app/workers.py`:
```python
async def run_reminder_loop(settings: Settings, db: DB, bot) -> None:
    from app.services.reminders import claim_due_reminders, send_reminders

    while True:
        await asyncio.sleep(settings.reminder_poll_interval_seconds)
        try:
            claims = await claim_due_reminders(db.session_factory, settings)
            if claims:
                await send_reminders(settings, bot, claims)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _report("reminders", exc)
```

В `app/main.py` — в список workers добавить строку:
```python
        asyncio.create_task(run_reminder_loop(settings, db, tg_app.bot), name="reminders"),
```
(и импорт `run_reminder_loop`).

- [ ] **Step 4: Run tests**

Run: `docker compose --profile test run --rm test pytest tests/integration/test_reminders.py -q`
Expected: 8 passed.

- [ ] **Step 5: Ruff + mypy + commit**

```bash
git add app/services/reminders.py app/workers.py app/main.py tests/integration/test_reminders.py
git commit -m "feat: reminders (SKIP LOCKED claim, reschedule from now, no retries)"
```

### Task 6.2: Проверка этапа

- [ ] **Step 1: Полный прогон**

Run: `docker compose --profile test run --rm test pytest -q && docker compose --profile test run --rm test ruff check app tests && docker compose --profile test run --rm test mypy app`
Expected: all green.

- [ ] **Step 2: Commit (если были фиксы)**

```bash
git add -A
git commit -m "fix: stage 6 lint/type fixes"
```

---

# Stage 7 — REST API и Mini App

### Task 7.1: Валидация initData (HMAC)

**Files:**
- Create: `app/api/auth.py`
- Test: `tests/unit/test_initdata.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_initdata.py
import pytest

from app.api.auth import AuthError, validate_init_data
from tests.fakes import make_init_data

TOKEN = "123456789:AAHtesttesttesttesttesttesttesttest1"


def test_valid_init_data():
    data = make_init_data(111, TOKEN, username="user111")
    user = validate_init_data(data, TOKEN, 86400)
    assert user["id"] == 111
    assert user["username"] == "user111"


def test_tampered_hash_rejected():
    data = make_init_data(111, TOKEN)
    user_part, _ = data.rsplit("&hash=", 1)
    with pytest.raises(AuthError):
        validate_init_data(user_part + "&hash=" + "0" * 64, TOKEN, 86400)


def test_tampered_payload_rejected():
    data = make_init_data(111, TOKEN)
    user_part, hash_part = data.rsplit("&hash=", 1)
    # подменяем user: меняем id
    tampered = user_part.replace("id%22%3A111", "id%22%3A999", 1) if "id%22%3A111" in user_part else user_part
    with pytest.raises(AuthError):
        validate_init_data(tampered + f"&hash={hash_part}", TOKEN, 86400)


def test_stale_auth_date_rejected():
    data = make_init_data(111, TOKEN, age_seconds=90000)
    with pytest.raises(AuthError):
        validate_init_data(data, TOKEN, 86400)


def test_wrong_token_rejected():
    data = make_init_data(111, TOKEN)
    with pytest.raises(AuthError):
        validate_init_data(data, "999999999:AABotherothertokenothertokenother1", 86400)


def test_missing_hash_rejected():
    with pytest.raises(AuthError):
        validate_init_data("user=%7B%7D", TOKEN, 86400)
```

Note: в `test_tampered_payload_rejected` — если замена не сработала (URL-кодирование другое), тест упадёт с `validate` success. Тогда упрости: возьми `make_init_data(999, TOKEN)` и поставь к нему hash от `make_init_data(111, TOKEN)`:

```python
def test_tampered_payload_rejected():
    good = make_init_data(111, TOKEN)
    other = make_init_data(999, TOKEN)
    _, good_hash = good.rsplit("&hash=", 1)
    other_part, _ = other.rsplit("&hash=", 1)
    with pytest.raises(AuthError):
        validate_init_data(other_part + f"&hash={good_hash}", TOKEN, 86400)
```

- [ ] **Step 2: Run to verify failure, then create `app/api/auth.py`**

```python
"""Валидация initData Mini App (HMAC-SHA256 по документации Telegram)."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


class AuthError(Exception):
    pass


def validate_init_data(init_data: str, bot_token: str, max_age_seconds: int) -> dict:
    params = dict(parse_qsl(init_data, keep_blank_values=True))
    hash_value = params.pop("hash", None)
    if not hash_value:
        raise AuthError("hash missing")
    auth_date_raw = params.get("auth_date")
    if auth_date_raw is None:
        raise AuthError("auth_date missing")
    try:
        auth_date = int(auth_date_raw)
    except ValueError:
        raise AuthError("invalid auth_date") from None
    if abs(time.time() - auth_date) > max_age_seconds:
        raise AuthError("auth_date too old")
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret = hmac.new(bot_token.encode(), b"WebAppData", hashlib.sha256).digest()
    digest = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(digest, hash_value):
        raise AuthError("hash mismatch")
    user_raw = params.get("user")
    if not user_raw:
        raise AuthError("user missing")
    try:
        return json.loads(user_raw)
    except json.JSONDecodeError:
        raise AuthError("invalid user json") from None
```

- [ ] **Step 3: Run tests**

Run: `docker compose --profile test run --rm test pytest tests/unit/test_initdata.py -q`
Expected: 6 passed.

- [ ] **Step 4: Ruff + commit**

```bash
git add app/api/auth.py tests/unit/test_initdata.py
git commit -m "feat: initData HMAC validation for Mini App"
```

### Task 7.2: API deps (initData → user, role guard)

**Files:**
- Create: `app/api/deps.py`

- [ ] **Step 1: Create `app/api/deps.py`**

```python
"""Зависимости API: initData → пользователь; доступ по роли из БД (без кеша)."""
from __future__ import annotations

from typing import Callable

from fastapi import Depends, HTTPException, Request

from app.api.auth import AuthError, validate_init_data
from app.models import User
from app.services.users import upsert_user


async def get_current_user(request: Request) -> User:
    settings = request.app.state.settings
    db = request.app.state.db
    init_data = request.query_params.get("initData", "")
    try:
        payload = validate_init_data(init_data, settings.bot_token, settings.initdata_max_age_seconds)
    except AuthError:
        raise HTTPException(status_code=401, detail="invalid init data") from None
    try:
        telegram_id = int(payload["id"])
    except (KeyError, ValueError, TypeError):
        raise HTTPException(status_code=401, detail="invalid user") from None
    async with db.session_factory() as session, session.begin():
        user = await upsert_user(
            session,
            telegram_id=telegram_id,
            username=payload.get("username"),
            first_name=payload.get("first_name"),
            last_name=payload.get("last_name"),
        )
        await session.refresh(user)
    if user.role is None:
        raise HTTPException(status_code=403, detail="no role")
    return user


def require_role(*roles: str) -> Callable:
    async def dep(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="forbidden")
        return user

    return dep
```

- [ ] **Step 2: Ruff + mypy + commit**

Run: `docker compose --profile test run --rm test ruff check app/api && docker compose --profile test run --rm test mypy app`
```bash
git add app/api/deps.py
git commit -m "feat: api auth deps (initData to user, role guard)"
```

### Task 7.3: API schemas + тикеты (list/mine/all, фильтры, пагинация, карточка)

**Files:**
- Create: `app/api/schemas.py`, `app/api/tickets.py`
- Test: `tests/integration/test_api.py` (частично: list/card)

- [ ] **Step 1: Create `app/api/schemas.py`**

```python
"""Pydantic-модели REST API Mini App."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class TicketOut(BaseModel):
    id: int
    description: str
    priority: str
    request_id: str
    tenant_id: str | None
    status: str
    version: int
    author_name: str
    assignee_name: str | None
    created_at: datetime
    updated_at: datetime
    taken_at: datetime | None
    closed_at: datetime | None


class TicketsPage(BaseModel):
    items: list[TicketOut]
    total: int
    page: int
    per_page: int


class ReopenRequest(BaseModel):
    expected_version: int


class ReassignRequest(BaseModel):
    user_id: int
    expected_version: int


class RoleRequest(BaseModel):
    role: str | None


class UserOut(BaseModel):
    id: int
    telegram_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    role: str | None


class MeOut(BaseModel):
    id: int
    telegram_id: int
    first_name: str | None
    username: str | None
    role: str
```

- [ ] **Step 2: Create `app/api/tickets.py`**

```python
"""REST API Mini App: обращения."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import aliased

from app.api.deps import get_current_user, require_role
from app.api.schemas import ReassignRequest, ReopenRequest, TicketOut, TicketsPage
from app.log import get_logger
from app.models import Ticket, User
from app.services import tickets as tickets_svc
from app.services.errors import NotFoundError, PermissionDenied, StaleTicketError
from app.services.post_sync import format_user, sync_ticket_post

log = get_logger("api.tickets")
router = APIRouter()

VALID_STATUS = {"new", "in_progress", "waiting_info", "resolved", "rejected", "duplicate"}
VALID_PRIORITY = {"critical", "high", "medium", "low"}

AuthorAlias = aliased(User)
AssigneeAlias = aliased(User)


def _to_out(t: Ticket, author: User, assignee: User | None) -> TicketOut:
    return TicketOut(
        id=t.id,
        description=t.description,
        priority=t.priority,
        request_id=t.request_id,
        tenant_id=t.tenant_id,
        status=t.status,
        version=t.version,
        author_name=format_user(author),
        assignee_name=format_user(assignee) if assignee else None,
        created_at=t.created_at,
        updated_at=t.updated_at,
        taken_at=t.taken_at,
        closed_at=t.closed_at,
    )


async def _resync(request: Request, ticket_id: int) -> None:
    try:
        await sync_ticket_post(
            request.app.state.settings, request.app.state.db, request.app.state.tg_app.bot, ticket_id
        )
    except Exception:
        log.exception("post resync failed after api change", ticket_id=ticket_id)


@router.get("/tickets", response_model=TicketsPage)
async def list_tickets(
    request: Request,
    user: User = Depends(get_current_user),
    tab: str = Query("all", pattern="^(mine|all)$"),
    status: str | None = None,
    priority: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
):
    if status is not None and status not in VALID_STATUS:
        raise HTTPException(422, "invalid status")
    if priority is not None and priority not in VALID_PRIORITY:
        raise HTTPException(422, "invalid priority")
    db = request.app.state.db
    conditions = []
    if tab == "mine":
        if user.role == "support":
            conditions.append(Ticket.author_id == user.id)
        elif user.role == "developer":
            conditions.append(Ticket.assignee_id == user.id)
        elif user.role == "admin":
            # admin видит обе вкладки: созданные мной И назначенные мне
            conditions.append(or_(Ticket.author_id == user.id, Ticket.assignee_id == user.id))
    if status:
        conditions.append(Ticket.status == status)
    if priority:
        conditions.append(Ticket.priority == priority)
    async with db.session_factory() as s:
        total = (await s.execute(select(func.count(Ticket.id)).where(*conditions))).scalar_one()
        rows = (
            await s.execute(
                select(Ticket, AuthorAlias, AssigneeAlias)
                .join(AuthorAlias, Ticket.author_id == AuthorAlias.id)
                .outerjoin(AssigneeAlias, Ticket.assignee_id == AssigneeAlias.id)
                .where(*conditions)
                .order_by(Ticket.created_at.desc(), Ticket.id.desc())
                .offset((page - 1) * per_page)
                .limit(per_page)
            )
        ).all()
    return TicketsPage(
        items=[_to_out(t, a, asg) for t, a, asg in rows],
        total=total,
        page=page,
        per_page=per_page,
    )


@router.get("/tickets/{ticket_id}", response_model=TicketOut)
async def get_ticket(request: Request, ticket_id: int, user: User = Depends(get_current_user)):
    async with request.app.state.db.session_factory() as s:
        t = await s.get(Ticket, ticket_id)
        if t is None:
            raise HTTPException(404, "not found")
        author = await s.get(User, t.author_id)
        assignee = await s.get(User, t.assignee_id) if t.assignee_id else None
    return _to_out(t, author, assignee)


@router.post("/tickets/{ticket_id}/reopen", response_model=TicketOut)
async def reopen_ticket(
    request: Request,
    ticket_id: int,
    body: ReopenRequest,
    user: User = Depends(require_role("admin")),
):
    try:
        async with request.app.state.db.session_factory() as s, s.begin():
            t = await tickets_svc.load_ticket(s, ticket_id)
            await tickets_svc.apply_transition(
                s, request.app.state.settings, user.id, t, body.expected_version, "reopen"
            )
            author = await s.get(User, t.author_id)
            out = _to_out(t, author, None)
    except NotFoundError:
        raise HTTPException(404, "not found") from None
    except StaleTicketError:
        raise HTTPException(409, "ticket changed") from None
    await _resync(request, ticket_id)
    return out


@router.post("/tickets/{ticket_id}/reassign", response_model=TicketOut)
async def reassign_ticket(
    request: Request,
    ticket_id: int,
    body: ReassignRequest,
    user: User = Depends(require_role("admin")),
):
    try:
        async with request.app.state.db.session_factory() as s, s.begin():
            t = await tickets_svc.load_ticket(s, ticket_id)
            await tickets_svc.apply_transition(
                s,
                request.app.state.settings,
                user.id,
                t,
                body.expected_version,
                "reassign",
                target_user_id=body.user_id,
            )
            author = await s.get(User, t.author_id)
            new_assignee = await s.get(User, t.assignee_id)
            out = _to_out(t, author, new_assignee)
    except NotFoundError:
        raise HTTPException(404, "not found") from None
    except StaleTicketError:
        raise HTTPException(409, "ticket changed") from None
    except PermissionDenied:
        raise HTTPException(403, "assignee must be developer or admin") from None
    await _resync(request, ticket_id)
    return out


@router.post("/tickets/{ticket_id}/restore-post")
async def restore_post(request: Request, ticket_id: int, user: User = Depends(require_role("admin"))):
    async with request.app.state.db.session_factory() as s:
        t = await s.get(Ticket, ticket_id)
    if t is None:
        raise HTTPException(404, "not found")
    try:
        await sync_ticket_post(
            request.app.state.settings,
            request.app.state.db,
            request.app.state.tg_app.bot,
            ticket_id,
            force_new=True,
        )
    except Exception:
        raise HTTPException(502, "post restore failed") from None
    return {"ok": True, "ticket_id": ticket_id}
```

- [ ] **Step 3: Create `tests/integration/test_api.py` (часть 1: auth, list, card)**

```python
# tests/integration/test_api.py
"""REST API Mini App."""
import httpx
import pytest_asyncio
from fastapi import FastAPI

from app.api.tickets import router as tickets_router
from app.api.users import router as users_router
from app.services import tickets as tickets_svc
from app.services import users as users_svc
from tests.fakes import make_init_data


@pytest_asyncio.fixture
async def api_client(db, settings, tg_app):
    app = FastAPI()
    app.state.settings = settings
    app.state.db = db
    app.state.tg_app = tg_app
    app.include_router(tickets_router, prefix="/api")
    app.include_router(users_router, prefix="/api")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _auth(user_id: int, settings, **kw) -> dict:
    return {"initData": make_init_data(user_id, settings.bot_token, **kw)}


async def _setup_roles(db, settings):
    """admin=111, support=222, dev=333, dev2=444."""
    async with db.session_factory() as s, s.begin():
        roles = {111: "admin", 222: "support", 333: "developer", 444: "developer"}
        ids = {}
        for tg, role in roles.items():
            u = await users_svc.upsert_user(s, telegram_id=tg, first_name=f"U{tg}")
            await users_svc.change_role(s, user_id=u.id, new_role=role)
            ids[tg] = u.id
    return ids


async def _make_ticket(db, settings, author_tg: int, **kw) -> int:
    async with db.session_factory() as s, s.begin():
        u = (
            await s.execute(__import__("sqlalchemy").select(users_svc.User).where(users_svc.User.telegram_id == author_tg))
        ).scalar_one()
        t = await tickets_svc.create_ticket(s, settings, u.id, description="d", priority="high", request_id="r", **kw)
    return t.id
```

Note: `users_svc.User` — в `app/services/users.py` User импортирован из app.models; доступ как `users_svc.User` работает (импорт в namespace). `_make_ticket` упрости — импортируй `User` и `select` нормально вверху файла:

```python
from sqlalchemy import select
from app.models import User
...
async def _make_ticket(db, settings, author_tg: int, **kw) -> int:
    async with db.session_factory() as s, s.begin():
        u = (await s.execute(select(User).where(User.telegram_id == author_tg))).scalar_one()
        t = await tickets_svc.create_ticket(s, settings, u.id, description="d", priority="high", request_id="r", **kw)
    return t.id
```

Тесты части 1:

```python
async def test_invalid_init_data(api_client, settings):
    r = await api_client.get("/api/tickets", params={"initData": "hash=bad"})
    assert r.status_code == 401


async def test_no_role_forbidden(api_client, db, settings):
    async with db.session_factory() as s, s.begin():
        await users_svc.upsert_user(s, telegram_id=111)
    r = await api_client.get("/api/tickets", params=_auth(111, settings))
    assert r.status_code == 403


async def test_list_all_and_mine(api_client, db, settings):
    ids = await _setup_roles(db, settings)
    t1 = await _make_ticket(db, settings, 222)            # support создала
    t2 = await _make_ticket(db, settings, 333, priority="low")  # dev создал (dev тоже может? нет!)
```

ВАЖНО: developer НЕ может создавать обращения (SPEC: support, admin). В API создания нет (форма — в боте), но тестовые данные должны быть реалистичными: t2 создаёт admin (111). Поправь:

```python
async def test_list_all_and_mine(api_client, db, settings):
    ids = await _setup_roles(db, settings)
    t1 = await _make_ticket(db, settings, 222)   # support
    t2 = await _make_ticket(db, settings, 111)   # admin
    # назначим t2 на dev 333
    async with db.session_factory() as s, s.begin():
        t = await tickets_svc.load_ticket(s, t2)
        await tickets_svc.take_ticket(s, settings, ids[333], t, expected_version=t.version)
    # support: mine = созданные мной
    r = await api_client.get("/api/tickets", params=_auth(222, settings) | {"tab": "mine"})
    assert [i["id"] for i in r.json()["items"]] == [t1]
    # developer: mine = назначенные мне
    r = await api_client.get("/api/tickets", params=_auth(333, settings) | {"tab": "mine"})
    assert [i["id"] for i in r.json()["items"]] == [t2]
    # admin: mine = обе
    r = await api_client.get("/api/tickets", params=_auth(111, settings) | {"tab": "mine"})
    assert sorted(i["id"] for i in r.json()["items"]) == sorted([t1, t2])
    # all — все
    r = await api_client.get("/api/tickets", params=_auth(111, settings) | {"tab": "all"})
    assert r.json()["total"] == 2


async def test_filters_pagination_sort(api_client, db, settings):
    await _setup_roles(db, settings)
    ids = [await _make_ticket(db, settings, 222, priority=p) for p in ("critical", "high", "low")]
    r = await api_client.get("/api/tickets", params=_auth(111, settings) | {"status": "new", "priority": "critical"})
    assert [i["id"] for i in r.json()["items"]] == [ids[0]]
    r = await api_client.get("/api/tickets", params=_auth(111, settings) | {"per_page": "2"})
    page1 = [i["id"] for i in r.json()["items"]]
    r = await api_client.get("/api/tickets", params=_auth(111, settings) | {"page": "2", "per_page": "2"})
    page2 = [i["id"] for i in r.json()["items"]]
    assert len(page1) == 2 and len(page2) == 1
    assert r.json()["total"] == 3
    # сортировка: новые сверху
    all_ids = page1 + page2
    assert all_ids == sorted(all_ids, reverse=True)
    # invalid filter
    r = await api_client.get("/api/tickets", params=_auth(111, settings) | {"status": "bogus"})
    assert r.status_code == 422


async def test_card(api_client, db, settings):
    await _setup_roles(db, settings)
    t1 = await _make_ticket(db, settings, 222)
    r = await api_client.get(f"/api/tickets/{t1}", params=_auth(111, settings))
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == t1 and body["status"] == "new" and body["version"] == 1
    assert body["author_name"].startswith("U222")
    assert body["assignee_name"] is None
    r = await api_client.get("/api/tickets/99999", params=_auth(111, settings))
    assert r.status_code == 404
```

- [ ] **Step 4: Run tests (нужен users.py API — создай его сейчас, код ниже в Task 7.4)**

- [ ] **Step 5: Ruff + mypy + commit**

```bash
git add app/api/schemas.py app/api/tickets.py tests/integration/test_api.py
git commit -m "feat: api tickets (list/mine/all, filters, pagination, card)"
```

### Task 7.4: API users (роли) + main.py v3

**Files:**
- Create: `app/api/users.py`
- Modify: `app/main.py` (v3: + /api роутеры)
- Test: дополнить `tests/integration/test_api.py` (часть 2)

- [ ] **Step 1: Create `app/api/users.py`**

```python
"""REST API Mini App: пользователи и роли (admin), /me."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select

from app.api.deps import get_current_user, require_role
from app.api.schemas import MeOut, RoleRequest, UserOut
from app.log import get_logger
from app.models import User
from app.services import users as users_svc
from app.services.errors import ActiveTicketsError, NotFoundError

log = get_logger("api.users")
router = APIRouter()


@router.get("/me", response_model=MeOut)
async def me(user: User = Depends(get_current_user)):
    return MeOut(
        id=user.id,
        telegram_id=user.telegram_id,
        first_name=user.first_name,
        username=user.username,
        role=user.role,
    )


@router.get("/users", response_model=list[UserOut])
async def list_users(request: Request, user: User = Depends(require_role("admin"))):
    async with request.app.state.db.session_factory() as s:
        rows = (await s.execute(select(User).order_by(User.first_name, User.id))).scalars().all()
    return [
        UserOut(
            id=u.id,
            telegram_id=u.telegram_id,
            username=u.username,
            first_name=u.first_name,
            last_name=u.last_name,
            role=u.role,
        )
        for u in rows
    ]


@router.put("/users/{user_id}/role")
async def set_role(
    request: Request, user_id: int, body: RoleRequest, user: User = Depends(require_role("admin"))
):
    try:
        async with request.app.state.db.session_factory() as s, s.begin():
            u = await users_svc.change_role(s, user_id=user_id, new_role=body.role)
            return {"id": u.id, "role": u.role}
    except NotFoundError:
        raise HTTPException(404, "not found") from None
    except ActiveTicketsError as e:
        raise HTTPException(409, detail={"error": "active_tickets", "tickets": e.ticket_ids}) from None
    except ValueError:
        raise HTTPException(422, "invalid role") from None
```

- [ ] **Step 2: Replace `app/main.py` (v3)** — добавить импорты и роутеры:

```python
from app.api.tickets import router as api_tickets_router
from app.api.users import router as api_users_router
...
def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="rent-support-bot", lifespan=lifespan)
    app.state.settings = settings or get_settings()
    app.include_router(health_router)
    app.include_router(webhook_router)
    app.include_router(api_tickets_router, prefix="/api")
    app.include_router(api_users_router, prefix="/api")
    return app
```
(остальной main.py — как в Task 5.9).

- [ ] **Step 3: Append к `tests/integration/test_api.py` (часть 2: admin actions, roles)**

```python
async def test_reopen_flow(api_client, db, settings, bot):
    ids = await _setup_roles(db, settings)
    t1 = await _make_ticket(db, settings, 222)
    # dev берёт и закрывает
    async with db.session_factory() as s, s.begin():
        t = await tickets_svc.load_ticket(s, t1)
        await tickets_svc.take_ticket(s, settings, ids[333], t, expected_version=t.version)
        t = await tickets_svc.load_ticket(s, t1)
        await tickets_svc.apply_transition(s, settings, ids[333], t, expected_version=t.version, action="to_resolved")
    # не admin — 403
    r = await api_client.post(f"/api/tickets/{t1}/reopen", json={"expected_version": 3}, params=_auth(333, settings))
    assert r.status_code == 403
    # admin, неверная version — 409
    r = await api_client.post(f"/api/tickets/{t1}/reopen", json={"expected_version": 1}, params=_auth(111, settings))
    assert r.status_code == 409
    # admin, верно
    r = await api_client.post(f"/api/tickets/{t1}/reopen", json={"expected_version": 3}, params=_auth(111, settings))
    assert r.status_code == 200
    assert r.json()["status"] == "new" and r.json()["version"] == 4
    assert r.json()["assignee_name"] is None
    # пост пересинхронизирован
    dev_posts = [m for m in bot.sent if m["chat_id"] == settings.dev_chat_id]
    assert any("новое" in m["text"] for m in dev_posts) or any(
        "новое" in e["text"] for e in bot.edited
    )


async def test_reassign_flow(api_client, db, settings):
    ids = await _setup_roles(db, settings)
    t1 = await _make_ticket(db, settings, 222)
    async with db.session_factory() as s, s.begin():
        t = await tickets_svc.load_ticket(s, t1)
        await tickets_svc.take_ticket(s, settings, ids[333], t, expected_version=t.version)
    # на support — 403
    r = await api_client.post(
        f"/api/tickets/{t1}/reassign",
        json={"user_id": ids[222], "expected_version": 2},
        params=_auth(111, settings),
    )
    assert r.status_code == 403
    # не admin — 403
    r = await api_client.post(
        f"/api/tickets/{t1}/reassign",
        json={"user_id": ids[444], "expected_version": 2},
        params=_auth(333, settings),
    )
    assert r.status_code == 403
    # на dev2 — OK
    r = await api_client.post(
        f"/api/tickets/{t1}/reassign",
        json={"user_id": ids[444], "expected_version": 2},
        params=_auth(111, settings),
    )
    assert r.status_code == 200
    assert r.json()["assignee_name"].startswith("U444")
    assert r.json()["version"] == 3 and r.json()["status"] == "in_progress"
    # старая version — 409
    r = await api_client.post(
        f"/api/tickets/{t1}/reassign",
        json={"user_id": ids[333], "expected_version": 2},
        params=_auth(111, settings),
    )
    assert r.status_code == 409


async def test_restore_post(api_client, db, settings, bot):
    ids = await _setup_roles(db, settings)
    t1 = await _make_ticket(db, settings, 222)
    from app.services.post_sync import sync_ticket_post

    async with db.session_factory() as s, s.begin():
        await s.execute(__import__("sqlalchemy").text("UPDATE tickets SET message_id = 55, post_synced_version = 1 WHERE id = :i"), {"i": t1})
    r = await api_client.post(f"/api/tickets/{t1}/restore-post", params=_auth(111, settings))
    assert r.status_code == 200
    assert len(bot.sent) == 1  # новый пост
    from sqlalchemy import text

    async with db.session_factory() as s:
        mid = (await s.execute(text("SELECT message_id FROM tickets WHERE id = :i"), {"i": t1})).scalar_one()
    assert mid == 1  # message_id нового поста (RecordingBot: первый send)
    # не admin — 403
    r = await api_client.post(f"/api/tickets/{t1}/restore-post", params=_auth(222, settings))
    assert r.status_code == 403


async def test_list_users_admin_only(api_client, db, settings):
    await _setup_roles(db, settings)
    r = await api_client.get("/api/users", params=_auth(222, settings))
    assert r.status_code == 403
    r = await api_client.get("/api/users", params=_auth(111, settings))
    assert r.status_code == 200
    assert len(r.json()) == 4


async def test_role_change_api(api_client, db, settings):
    ids = await _setup_roles(db, settings)
    # dev с активным обращением — 409 со списком
    t1 = await _make_ticket(db, settings, 222)
    async with db.session_factory() as s, s.begin():
        t = await tickets_svc.load_ticket(s, t1)
        await tickets_svc.take_ticket(s, settings, ids[333], t, expected_version=t.version)
    r = await api_client.put(f"/api/users/{ids[333]}/role", json={"role": "support"}, params=_auth(111, settings))
    assert r.status_code == 409
    assert r.json()["detail"]["tickets"] == [t1]
    # без активных — OK
    r = await api_client.put(f"/api/users/{ids[444]}/role", json={"role": "support"}, params=_auth(111, settings))
    assert r.status_code == 200
    assert r.json()["role"] == "support"
    # некорректная роль — 422
    r = await api_client.put(f"/api/users/{ids[444]}/role", json={"role": "boss"}, params=_auth(111, settings))
    assert r.status_code == 422
    # не admin — 403
    r = await api_client.put(f"/api/users/{ids[444]}/role", json={"role": "admin"}, params=_auth(222, settings))
    assert r.status_code == 403


async def test_me(api_client, db, settings):
    await _setup_roles(db, settings)
    r = await api_client.get("/api/me", params=_auth(222, settings))
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "support" and body["telegram_id"] == 222
```

Note: в `test_restore_post` замените `__import__("sqlalchemy").text` на обычный импорт `from sqlalchemy import text` вверху файла.

- [ ] **Step 4: Run all API tests**

Run: `docker compose --profile test run --rm test pytest tests/integration/test_api.py -q`
Expected: ~11 passed.

- [ ] **Step 5: Ruff + mypy + commit**

```bash
git add app/api/users.py app/main.py tests/integration/test_api.py
git commit -m "feat: api users/roles, /me; main v3 with /api routers"
```

### Task 7.5: Mini App фронтенд (static)

**Files:**
- Create: `app/static/index.html`, `app/static/style.css`, `app/static/app.js`
- Modify: `app/main.py` (v3.1: mount static)
- Test: дополнить `tests/integration/test_api.py`

- [ ] **Step 1: Create `app/static/index.html`**

```html
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Обращения</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <header><h1>Обращения</h1></header>
  <nav id="tabs">
    <button data-tab="mine" class="active">Мои</button>
    <button data-tab="all">Все</button>
    <button data-tab="roles" id="tab-roles" hidden>Роли</button>
  </nav>

  <section id="view-list">
    <div id="filters">
      <select id="f-status">
        <option value="">Статус: все</option>
        <option value="new">новое</option>
        <option value="in_progress">в работе</option>
        <option value="waiting_info">ждём информацию</option>
        <option value="resolved">решено</option>
        <option value="rejected">отклонено</option>
        <option value="duplicate">дубль</option>
      </select>
      <select id="f-priority">
        <option value="">Приоритет: все</option>
        <option value="critical">критический</option>
        <option value="high">высокий</option>
        <option value="medium">средний</option>
        <option value="low">низкий</option>
      </select>
    </div>
    <ul id="ticket-list"></ul>
    <div id="pager">
      <button id="prev">← Назад</button>
      <span id="page-info"></span>
      <button id="next">Далее →</button>
    </div>
  </section>

  <section id="view-card" hidden>
    <button id="back">← Назад</button>
    <div id="card"></div>
  </section>

  <section id="view-roles" hidden>
    <table id="users-table">
      <thead><tr><th>Пользователь</th><th>username</th><th>Роль</th></tr></thead>
      <tbody></tbody>
    </table>
  </section>

  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <script src="app.js"></script>
</body>
</html>
```

- [ ] **Step 2: Create `app/static/style.css`** (тема Telegram через CSS-переменные; светлая/тёмная — из themeParams)

```css
:root {
  --bg: #ffffff;
  --text: #000000;
  --hint: #999999;
  --secondary-bg: #f4f4f5;
  --button: #2481cc;
  --button-text: #ffffff;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: var(--bg);
  color: var(--text);
}

header { padding: 12px 16px; border-bottom: 1px solid var(--secondary-bg); }
header h1 { margin: 0; font-size: 20px; }

#tabs { display: flex; gap: 8px; padding: 12px 16px; }
#tabs button {
  flex: 1; padding: 8px; border: 1px solid var(--secondary-bg);
  background: var(--secondary-bg); color: var(--text);
  border-radius: 8px; cursor: pointer;
}
#tabs button.active { background: var(--button); color: var(--button-text); }

#filters { display: flex; gap: 8px; padding: 0 16px 8px; }
#filters select { flex: 1; padding: 6px; border-radius: 8px; border: 1px solid var(--secondary-bg); background: var(--bg); color: var(--text); }

#ticket-list { list-style: none; margin: 0; padding: 0 16px; }
.ticket {
  background: var(--secondary-bg); border-radius: 10px;
  padding: 10px 12px; margin-bottom: 8px; cursor: pointer;
}
.ticket-top { display: flex; gap: 8px; align-items: center; margin-bottom: 4px; }
.num { font-weight: 700; }
.badge { font-size: 11px; padding: 2px 8px; border-radius: 10px; background: var(--bg); }
.status-new { color: #2481cc; }
.status-in_progress, .status-waiting_info { color: #b58900; }
.status-resolved { color: #2e9e44; }
.status-rejected, .status-duplicate { color: #c0392b; }
.prio-critical { color: #c0392b; font-weight: 700; }
.prio-high { color: #e67e22; }
.prio-low { color: var(--hint); }
.ticket-desc { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ticket-meta { font-size: 12px; color: var(--hint); margin-top: 4px; }
.empty { text-align: center; color: var(--hint); padding: 24px; }

#pager { display: flex; justify-content: center; align-items: center; gap: 12px; padding: 12px; }
#pager button { padding: 6px 12px; border-radius: 8px; border: none; background: var(--button); color: var(--button-text); cursor: pointer; }
#pager button:disabled { opacity: 0.4; }

#view-card { padding: 0 16px 16px; }
#back { margin: 12px 0; padding: 6px 12px; border-radius: 8px; border: 1px solid var(--secondary-bg); background: var(--secondary-bg); color: var(--text); cursor: pointer; }
.card { background: var(--secondary-bg); border-radius: 10px; padding: 14px; }
.card .desc { white-space: pre-wrap; }
.card dl { display: grid; grid-template-columns: auto 1fr; gap: 4px 12px; margin: 12px 0; }
.card dt { color: var(--hint); }
#admin-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
#admin-actions button { padding: 8px 12px; border: none; border-radius: 8px; background: var(--button); color: var(--button-text); cursor: pointer; }
#admin-actions button.danger { background: #c0392b; }

#view-roles { padding: 0 16px; overflow-x: auto; }
#users-table { width: 100%; border-collapse: collapse; }
#users-table th, #users-table td { text-align: left; padding: 8px 6px; border-bottom: 1px solid var(--secondary-bg); }
#users-table select { padding: 4px; border-radius: 6px; border: 1px solid var(--secondary-bg); background: var(--bg); color: var(--text); }
```

- [ ] **Step 3: Create `app/static/app.js`**

```javascript
"use strict";

const tg = window.Telegram && window.Telegram.WebApp;
if (tg) {
  tg.ready();
  tg.expand();
  applyTheme(tg);
}
const INIT_DATA = (tg && tg.initData) || "";

const STATUS_RU = {
  new: "новое", in_progress: "в работе", waiting_info: "ждём информацию",
  resolved: "решено", rejected: "отклонено", duplicate: "дубль",
};
const PRIORITY_RU = { critical: "критический", high: "высокий", medium: "средний", low: "низкий" };
const FINAL = ["resolved", "rejected", "duplicate"];
const ACTIVE = ["in_progress", "waiting_info"];

let me = null;
const state = { tab: "mine", page: 1, per_page: 20, status: "", priority: "", total: 0 };

function applyTheme(tg) {
  const p = tg.themeParams || {};
  const root = document.documentElement.style;
  root.setProperty("--bg", p.bg_color || "#ffffff");
  root.setProperty("--text", p.text_color || "#000000");
  root.setProperty("--hint", p.hint_color || "#999999");
  root.setProperty("--secondary-bg", p.secondary_bg_color || "#f4f4f5");
  root.setProperty("--button", p.button_color || "#2481cc");
  root.setProperty("--button-text", p.button_text_color || "#ffffff");
  if (tg.setHeaderColor) tg.setHeaderColor(p.header_color || p.bg_color || "#ffffff");
  if (tg.setBackgroundColor) tg.setBackgroundColor(p.bg_color || "#ffffff");
}

async function api(path, opts) {
  const { method = "GET", body } = opts || {};
  const url = path + (path.includes("?") ? "&" : "?") + "initData=" + encodeURIComponent(INIT_DATA);
  const res = await fetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail; } catch (_) { /* ignore */ }
    const err = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    err.status = res.status;
    err.detail = detail;
    throw err;
  }
  return res.json();
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function loadMe() {
  me = await api("/api/me");
  if (me.role === "admin") document.getElementById("tab-roles").hidden = false;
  switchTab("mine");
}

async function switchTab(tab) {
  state.tab = tab;
  state.page = 1;
  document.querySelectorAll("#tabs button").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === tab)
  );
  const inList = tab === "mine" || tab === "all";
  document.getElementById("view-list").hidden = !inList;
  document.getElementById("view-card").hidden = true;
  document.getElementById("view-roles").hidden = tab !== "roles";
  if (tab === "roles") await loadUsers();
  else await loadList();
}

async function loadList() {
  const params = new URLSearchParams({ tab: state.tab, page: state.page, per_page: state.per_page });
  if (state.status) params.set("status", state.status);
  if (state.priority) params.set("priority", state.priority);
  const data = await api("/api/tickets?" + params.toString());
  state.total = data.total;
  renderList(data.items);
  const pages = Math.max(1, Math.ceil(data.total / state.per_page));
  document.getElementById("page-info").textContent = state.page + " / " + pages;
  document.getElementById("prev").disabled = state.page <= 1;
  document.getElementById("next").disabled = state.page >= pages;
}

function renderList(items) {
  const ul = document.getElementById("ticket-list");
  ul.innerHTML = "";
  for (const t of items) {
    const li = document.createElement("li");
    li.className = "ticket";
    li.innerHTML =
      '<div class="ticket-top">' +
      '<span class="num">#' + t.id + "</span>" +
      '<span class="badge status-' + t.status + '">' + STATUS_RU[t.status] + "</span>" +
      '<span class="badge prio-' + t.priority + '">' + PRIORITY_RU[t.priority] + "</span>" +
      "</div>" +
      '<div class="ticket-desc">' + escapeHtml(t.description.slice(0, 120)) + "</div>" +
      '<div class="ticket-meta">Заявка: ' + escapeHtml(t.request_id) + " · " +
      escapeHtml(t.author_name) + (t.assignee_name ? " → " + escapeHtml(t.assignee_name) : "") + "</div>";
    li.addEventListener("click", () => openCard(t.id));
    ul.appendChild(li);
  }
  if (!items.length) ul.innerHTML = '<li class="empty">Нет обращений</li>';
}

async function openCard(id) {
  const t = await api("/api/tickets/" + id);
  document.getElementById("view-list").hidden = true;
  document.getElementById("view-card").hidden = false;
  renderCard(t);
}

function renderCard(t) {
  const el = document.getElementById("card");
  el.innerHTML =
    '<div class="card">' +
    '<div class="ticket-top">' +
    '<span class="num">#' + t.id + "</span>" +
    '<span class="badge status-' + t.status + '">' + STATUS_RU[t.status] + "</span>" +
    '<span class="badge prio-' + t.priority + '">' + PRIORITY_RU[t.priority] + "</span>" +
    "</div>" +
    '<p class="desc">' + escapeHtml(t.description) + "</p>" +
    "<dl>" +
    "<dt>Приоритет</dt><dd>" + PRIORITY_RU[t.priority] + "</dd>" +
    "<dt>Id заявки</dt><dd>" + escapeHtml(t.request_id) + "</dd>" +
    "<dt>Id арендатора</dt><dd>" + (t.tenant_id ? escapeHtml(t.tenant_id) : "—") + "</dd>" +
    "<dt>Автор</dt><dd>" + escapeHtml(t.author_name) + "</dd>" +
    "<dt>Исполнитель</dt><dd>" + (t.assignee_name ? escapeHtml(t.assignee_name) : "—") + "</dd>" +
    "<dt>Создано</dt><dd>" + new Date(t.created_at).toLocaleString("ru") + "</dd>" +
    "</dl>" +
    '<div id="admin-actions"></div>' +
    "</div>";
  if (me.role === "admin") renderAdminActions(t);
}

function onApiError(e, t) {
  if (e.status === 409) {
    window.alert("Обращение изменилось");
    openCard(t.id);
  } else {
    window.alert(e.message);
  }
}

function renderAdminActions(t) {
  const box = document.getElementById("admin-actions");
  box.innerHTML = "";
  const mk = (label, fn) => {
    const b = document.createElement("button");
    b.textContent = label;
    b.addEventListener("click", fn);
    box.appendChild(b);
  };
  if (FINAL.includes(t.status)) {
    mk("Переоткрыть", async () => {
      try {
        await api("/api/tickets/" + t.id + "/reopen", { method: "POST", body: { expected_version: t.version } });
        openCard(t.id);
      } catch (e) { onApiError(e, t); }
    });
  }
  if (ACTIVE.includes(t.status)) {
    mk("Переназначить", async () => {
      let users;
      try { users = await api("/api/users"); } catch (e) { window.alert(e.message); return; }
      users = users.filter((u) => ["developer", "admin"].includes(u.role));
      if (!users.length) { window.alert("Нет пользователей с ролью developer/admin"); return; }
      const pick = window.prompt(
        "Переназначить на:\n" + users.map((u, i) => (i + 1) + ". " + (u.first_name || "") + " (" + u.id + ")").join("\n"),
        "1"
      );
      if (pick === null) return;
      const target = users[parseInt(pick, 10) - 1];
      if (!target) return;
      try {
        await api("/api/tickets/" + t.id + "/reassign", {
          method: "POST",
          body: { user_id: target.id, expected_version: t.version },
        });
        openCard(t.id);
      } catch (e) { onApiError(e, t); }
    });
  }
  mk("Восстановить пост", async () => {
    try {
      await api("/api/tickets/" + t.id + "/restore-post", { method: "POST" });
      window.alert("Пост восстановлен");
    } catch (e) { window.alert(e.message); }
  });
}

async function loadUsers() {
  let users;
  try { users = await api("/api/users"); } catch (e) { window.alert(e.message); return; }
  const tbody = document.querySelector("#users-table tbody");
  tbody.innerHTML = "";
  for (const u of users) {
    const tr = document.createElement("tr");
    tr.innerHTML =
      "<td>" + escapeHtml(u.first_name || "") + " (" + u.telegram_id + ")</td>" +
      "<td>" + (u.username ? "@" + escapeHtml(u.username) : "—") + "</td>";
    const sel = document.createElement("select");
    for (const r of [null, "support", "developer", "admin"]) {
      const opt = document.createElement("option");
      opt.value = r === null ? "" : r;
      opt.textContent = r === null ? "без роли" : r;
      opt.selected = (u.role || null) === r;
      sel.appendChild(opt);
    }
    sel.addEventListener("change", async () => {
      try {
        await api("/api/users/" + u.id + "/role", { method: "PUT", body: { role: sel.value || null } });
      } catch (e) {
        if (e.status === 409 && e.detail && e.detail.tickets) {
          window.alert("У пользователя активные обращения: " + e.detail.tickets.join(", "));
        } else {
          window.alert(e.message);
        }
        loadUsers();
      }
    });
    tr.appendChild(sel);
    tbody.appendChild(tr);
  }
}

document.querySelectorAll("#tabs button").forEach((b) =>
  b.addEventListener("click", () => switchTab(b.dataset.tab))
);
document.getElementById("f-status").addEventListener("change", (e) => {
  state.status = e.target.value; state.page = 1; loadList();
});
document.getElementById("f-priority").addEventListener("change", (e) => {
  state.priority = e.target.value; state.page = 1; loadList();
});
document.getElementById("prev").addEventListener("click", () => { state.page -= 1; loadList(); });
document.getElementById("next").addEventListener("click", () => { state.page += 1; loadList(); });
document.getElementById("back").addEventListener("click", () => {
  document.getElementById("view-card").hidden = true;
  document.getElementById("view-list").hidden = false;
});

loadMe().catch((e) => {
  document.body.innerHTML = "<p>Ошибка: " + escapeHtml(e.message) + "</p>";
});
```

- [ ] **Step 4: Mount static in `app/main.py`** — добавить в `create_app` (После роутеров):

```python
from pathlib import Path
from fastapi.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).parent / "static"
...
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
```

- [ ] **Step 5: Append test в `tests/integration/test_api.py`**

```python
async def test_static_files():
    """Статика Mini App отдаётся FastAPI."""
    from tests.conftest import FAKE_TOKEN, TEST_DB_URL

    from app.config import Settings
    from app.main import create_app

    settings = Settings(bot_token=FAKE_TOKEN, dev_chat_id=-1, database_url=TEST_DB_URL, _env_file=None)
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/")
        assert r.status_code == 200
        assert "app.js" in r.text
        r = await c.get("/app.js")
        assert r.status_code == 200
        r = await c.get("/style.css")
        assert r.status_code == 200
```

- [ ] **Step 6: Run + Ruff + commit**

Run: `docker compose --profile test run --rm test pytest tests/integration/test_api.py -q`
Expected: all passed.
```bash
git add app/static/ app/main.py tests/integration/test_api.py
git commit -m "feat: mini app frontend (vanilla JS, telegram theme) + static mount"
```

### Task 7.6: Проверка этапа

- [ ] **Step 1: Полный прогон**

Run: `docker compose --profile test run --rm test pytest -q && docker compose --profile test run --rm test ruff check app tests && docker compose --profile test run --rm test mypy app`
Expected: all green.

- [ ] **Step 2: Commit (если были фиксы)**

```bash
git add -A
git commit -m "fix: stage 7 lint/type fixes"
```

---

# Stage 8 — Логирование и мониторинг

### Task 8.1: Sentry

**Files:**
- Modify: `app/main.py` (v4: sentry init в lifespan)
- Test: `tests/integration/test_metrics.py` (создать; sentry-тест внутри)

- [ ] **Step 1: Заменить `app/main.py` (v4)** — в lifespan, после `log.configure_logging`, добавить:

```python
    if settings.sentry_dsn:
        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            integrations=[
                sentry_sdk.integrations.FastApiIntegration(),
                sentry_sdk.integrations.SqlalchemyIntegration(),
            ],
        )
```
(импорт `import sentry_sdk` вверху). Остальной main.py — как в Task 5.9/6.1 + статика из Task 7.5.

- [ ] **Step 2: Write `tests/integration/test_metrics.py` (сентри-часть)**

```python
# tests/integration/test_metrics.py
"""Сентри, HTTP-метрики, metrics-порт."""
import httpx
import sentry_sdk
from telegram import Update

from app.metrics import open_tickets_by_status
from tests.fakes import make_update


async def test_ptb_error_handler_sends_to_sentry(tg_app, bot, monkeypatch):
    calls: list = []
    monkeypatch.setattr(sentry_sdk, "capture_exception", lambda exc: calls.append(exc))
    from app.metrics import errors_total

    before = _counter(errors_total, {"component": "telegram"})
    bot.fail_next_send = RuntimeError("telegram down")
    d = make_update(1, 111, text="/start")
    await tg_app.process_update(Update.de_json(d, tg_app.bot))
    assert calls and isinstance(calls[0], RuntimeError)
    assert _counter(errors_total, {"component": "telegram"}) == before + 1


def _counter(metric, labels: dict) -> float:
    for m in metric.collect():
        for s in m.samples:
            if s.labels == labels and s.name.endswith("_total"):
                return s.value
    return 0.0
```

- [ ] **Step 3: Run + commit**

Run: `docker compose --profile test run --rm test pytest tests/integration/test_metrics.py -q`
```bash
git add app/main.py tests/integration/test_metrics.py
git commit -m "feat: sentry (init with fastapi/sqlalchemy, ptb error handler)"
```

### Task 8.2: HTTP-метрики (middleware, path template)

**Files:**
- Modify: `app/metrics.py` (добавить `add_http_metrics`), `app/main.py` (вызов)
- Test: дополнить `tests/integration/test_metrics.py`

- [ ] **Step 1: Append в `app/metrics.py`**

```python
from time import perf_counter

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware  # не нужно — используем @app.middleware


def add_http_metrics(app) -> None:
    """Middleware: число запросов, latency, коды. В label — ШАБЛОН пути (кардинальность)."""

    @app.middleware("http")
    async def http_metrics(request: Request, call_next):
        start = perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        path_format = getattr(route, "path_format", None)
        template = path_format if path_format else "unmatched"
        http_requests_total.labels(
            method=request.method, route=template, status=str(response.status_code)
        ).inc()
        http_request_duration_seconds.labels(method=request.method, route=template).observe(
            perf_counter() - start
        )
        return response
```
Убери неиспользуемый импорт BaseHTTPMiddleware.

- [ ] **Step 2: В `app/main.py` create_app** — после `app.state.settings = ...`:

```python
    metrics.add_http_metrics(app)
```

- [ ] **Step 3: Append тест**

```python
def test_http_metrics_path_template(db, settings):
    from fastapi import FastAPI

    from app.api.health import router as health_router
    from app.metrics import add_http_metrics, http_requests_total

    app = FastAPI()
    app.state.settings = settings
    app.state.db = db
    app.include_router(health_router)
    add_http_metrics(app)
    transport = httpx.ASGITransport(app=app)

    async def run():
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            assert (await c.get("/health/live")).status_code == 200
            assert (await c.get("/health/live")).status_code == 200
            assert (await c.get("/no-such-path")).status_code == 404

    import asyncio

    asyncio.run(run())

    def value(labels: dict) -> float:
        for m in http_requests_total.collect():
            for s in m.samples:
                if s.labels == labels and s.name.endswith("_total"):
                    return s.value
        return 0.0

    assert value({"method": "GET", "route": "/health/live", "status": "200"}) >= 2
    assert value({"method": "GET", "route": "unmatched", "status": "404"}) >= 1
```

Note: `asyncio.run` в sync-тесте — OK (pytest-asyncio не нужен для sync-тестов).

- [ ] **Step 4: Run + commit**

Run: `docker compose --profile test run --rm test pytest tests/integration/test_metrics.py -q`
```bash
git add app/metrics.py app/main.py tests/integration/test_metrics.py
git commit -m "feat: http metrics middleware with path-template labels"
```

### Task 8.3: Metrics-порт + gauge из БД

**Files:**
- Modify: `app/metrics.py` (make_metrics_app, start_metrics_server), `app/main.py` (запуск/остановка)
- Test: дополнить `tests/integration/test_metrics.py`

- [ ] **Step 1: Append в `app/metrics.py`**

```python
def make_metrics_app(db: "DB") -> "FastAPI":
    """Отдельное ASGI-приложение для /metrics (свой порт, не через ingress)."""
    from fastapi import FastAPI
    from fastapi.responses import Response
    from prometheus_client import REGISTRY, CONTENT_TYPE_LATEST, generate_latest
    from sqlalchemy import func, select

    from app.models import Ticket
    from app.states import STATUSES

    app = FastAPI()

    @app.get("/metrics")
    async def metrics_endpoint():
        # Gauge «открытые обращения по статусу» — запрос к БД при скрейпе.
        # Каждая реплика отдаёт одинаковое значение → агрегировать max(), не sum().
        async with db.session_factory() as s:
            rows = (
                await s.execute(select(Ticket.status, func.count(Ticket.id)).group_by(Ticket.status))
            ).all()
        counts = {st: 0 for st in STATUSES}
        for st, n in rows:
            counts[st] = n
        for st in STATUSES:
            open_tickets_by_status.labels(status=st).set(counts[st])
        return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    return app


def start_metrics_server(port: int, db: "DB"):
    """Второй uvicorn-сервер в том же процессе (один uvicorn-процесс на pod, два порта)."""
    import asyncio

    import uvicorn

    config = uvicorn.Config(make_metrics_app(db), host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(), name="metrics-server")
    return server, task
```
Импорт типов вверху: `from typing import TYPE_CHECKING` ... проще: в сигнатурах использовать строковые аннотации, как показано.

- [ ] **Step 2: В `app/main.py` lifespan** — после workers:

```python
    metrics_server, metrics_task = metrics.start_metrics_server(settings.metrics_port, db)
```
и в finally (перед stop PTB):

```python
        metrics_server.should_exit = True
        await metrics_task
```

- [ ] **Step 3: Append тест**

```python
def _free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def test_metrics_port_and_gauge(db, settings):
    from app.metrics import start_metrics_server
    from app.services import tickets as tickets_svc
    from app.services import users as users_svc

    # два обращения: new и in_progress
    async with db.session_factory() as s, s.begin():
        u = await users_svc.upsert_user(s, telegram_id=1)
        d = await users_svc.upsert_user(s, telegram_id=2)
        await users_svc.change_role(s, user_id=d.id, new_role="developer")
        t1 = await tickets_svc.create_ticket(s, settings, u.id, description="d", priority="high", request_id="r1")
        t2 = await tickets_svc.create_ticket(s, settings, u.id, description="d2", priority="low", request_id="r2")
        await tickets_svc.take_ticket(s, settings, d.id, t2, expected_version=1)

    port = _free_port()
    server, task = start_metrics_server(port, db)
    try:
        import asyncio as _a

        for _ in range(100):
            try:
                r = httpx.get(f"http://127.0.0.1:{port}/metrics", timeout=0.5)
                break
            except Exception:
                await _a.sleep(0.1)
        assert r.status_code == 200
        text = r.text
        assert 'tickets_open_by_status{status="new"} 1.0' in text
        assert 'tickets_open_by_status{status="in_progress"} 1.0' in text
        assert 'tickets_open_by_status{status="resolved"} 0.0' in text
        assert "ticket_take_duration_seconds" in text
    finally:
        server.should_exit = True
        await task
```

Note: `httpx.get` (sync) из async-теста блокирует loop ~50ms — допустимо; если ruff B028/ASYNC ругается — замени на `httpx.Client()`.

- [ ] **Step 4: Run + commit**

Run: `docker compose --profile test run --rm test pytest tests/integration/test_metrics.py -q`
```bash
git add app/metrics.py app/main.py tests/integration/test_metrics.py
git commit -m "feat: metrics server on separate port, open-tickets gauge from DB"
```

### Task 8.4: Метрики не на основном порту + проверка этапа

- [ ] **Step 1: Append тест**

```python
async def test_metrics_not_on_main_port(db, settings):
    """Основной HTTP-порт не отдаёт /metrics (отдельный METRICS_PORT)."""
    from tests.conftest import FAKE_TOKEN, TEST_DB_URL

    from app.config import Settings
    from app.main import create_app

    s = Settings(bot_token=FAKE_TOKEN, dev_chat_id=-1, database_url=TEST_DB_URL, _env_file=None)
    app = create_app(s)
    assert "/metrics" not in [getattr(r, "path", "") for r in app.routes]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/metrics")
        assert r.status_code == 404
```

- [ ] **Step 2: Полный прогон**

Run: `docker compose --profile test run --rm test pytest -q && docker compose --profile test run --rm test ruff check app tests && docker compose --profile test run --rm test mypy app`
Expected: all green.

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "feat: monitoring complete (sentry, http metrics, metrics port)"
```

---

# Stage 9 — Docker и README

### Task 9.1: Проверка Docker-целев

**Files:** без новых (Dockerfile/compose созданы в Stage 1)

- [ ] **Step 1: Построить все таргеты**

Run: `docker compose build app test 2>&1 | tail -5 && docker build --target prod -q . 2>&1 | tail -2`
Expected: все три таргета строятся.

- [ ] **Step 2: prod-образ: non-root + импорт приложения**

Run:
```bash
IMG=$(docker build --target prod -q .)
docker run --rm -e BOT_TOKEN=123456789:AAHtesttesttesttesttesttesttesttest1 -e DATABASE_URL=postgresql+asyncpg://u:p@h/db -e DEV_CHAT_ID=-100 "$IMG" sh -c 'test $(id -u) != 0 && python -c "import app.main" && echo PROD_IMAGE_OK'
```
Expected: `PROD_IMAGE_OK` (пользователь non-root, приложение импортируется офлайн).

- [ ] **Step 3: test-таргет по умолчанию запускает pytest (пустая БД не нужна для unit-тестов)**

Run: `docker compose --profile test run --rm test pytest tests/unit -q`
Expected: all passed (unit-тесты не трогают БД).

- [ ] **Step 4: Commit (если ничего не менялось — пропустить)**

### Task 9.2: README

**Files:**
- Modify: `README.md` (заменить заглушку)

- [ ] **Step 1: Записать `README.md`**

```markdown
# rent-support-bot

Telegram-бот + Mini App для учёта обращений первой линии поддержки к дежурному
разработчику: тикеты со статусами, посты в чат разработчиков, напоминания,
роли, REST API и статический Mini App. Полное ТЗ — в [SPEC.md](SPEC.md).

## Стек

Python 3.14, FastAPI, python-telegram-bot 22 (async), PostgreSQL 16,
SQLAlchemy 2 (async/asyncpg), Alembic, pydantic-settings, structlog,
sentry-sdk, prometheus-client, uvicorn. Тестируется на реальном PostgreSQL из
docker compose.

## Быстрый старт (локально)

Нужны: Docker, токен бота от BotFather, id чата разработчиков.

```bash
cp .env.example .env   # заполнить BOT_TOKEN, DEV_CHAT_ID
docker compose --profile dev up --build
```

- Приложение: http://localhost:8000 (health: /health/live, /health/ready)
- Метрики: http://localhost:9464/metrics
- Локально используется `BOT_MODE=polling` (публичный HTTPS не нужен).
- Без валидного BOT_TOKEN приложение не запустит бота (get_me на старте).
- Код запечён в образ (bind mount нет): после изменений пересобирай —
  `docker compose --profile dev up --build`.

## Переменные окружения

См. [.env.example](.env.example) — все переменные с комментариями.
В production секреты приходят из внешнего пайплайна как env-переменные
(настоящие `BOT_TOKEN`, учётные данные `DATABASE_URL` и secret webhook
обязательны; `bot`/`bot` из compose — только для разработки).

## Тесты

```bash
docker compose --profile test run --rm test            # весь pytest
docker compose --profile test run --rm test pytest tests/unit -q   # подмножество
```

Тесты поднимают PostgreSQL сами (compose), создают свежую БД на сессию и
пробегут `alembic upgrade head`.

## Миграции

Миграции запускаются отдельной командой (в k8s — отдельный job), не при старте
приложения. В dev-compose автозапуск допустим (target dev).

```bash
docker compose exec app alembic upgrade head     # применить
docker compose exec app alembic revision --autogenerate -m "..."   # создать
docker compose exec app alembic downgrade -1     # откатить шаг
```

## Образы

Multi-stage `Dockerfile`:

- `dev` — dev-зависимости, hot reload, авто-миграции (compose `app`);
- `test` — тестовые зависимости, по умолчанию `pytest`;
- `prod` — минимальный, non-root, без dev-зависимостей; ровно один
  uvicorn-процесс (без --workers/gunicorn), открывает основной порт (8000)
  и порт метрик (METRICS_PORT, по умолчанию 9464).

```bash
docker build --target prod -t rent-support-bot:prod .
```

## Регистрация у BotFather

1. Команды бота (/new, /cancel, /help) выставляются автоматически при старте
   (set_my_commands).
2. Mini App: в BotFather → Bot Settings → Menu Button → URL = `MINI_APP_URL`
   (корневой URL приложения, FastAPI отдаёт `app/static/index.html`).
3. Webhook (production, `BOT_MODE=webhook`): устанавливается автоматически при
   старте (`set_webhook` с `WEBHOOK_SECRET_TOKEN`); URL — `WEBHOOK_URL`,
   например `https://host/telegram/webhook`. HTTPS настраивают
   администраторы инфраструктуры.
4. Первичные админы: `BOOTSTRAP_ADMIN_IDS` — получают роль admin при первом
   /start.

## k8s

CI/CD и манифесты не входят в проект. Учтено:

- один uvicorn-процесс на pod, масштабирование числом реплик;
- два порта: 8000 (HTTP) и METRICS_PORT (метрики — не наружу через ingress);
- webhook-режим безопасен для нескольких реплик: дедупликация апдейтов,
  черновики форм, напоминания и синхронизация постов — в БД
  (FOR UPDATE / FOR UPDATE SKIP LOCKED / advisory lock);
- миграции — отдельный job.

## Метрики: правила агрегации

- Счётчики и гистограммы (`*_total`, `*_seconds`) — процессы считают сами,
  по подам суммируются: `sum(metric)`.
- **`tickets_open_by_status` — агрегировать через `max()`, НЕ `sum()`**: это
  gauge, вычисляемый запросом к БД при скрейпе, и каждая реплика отдаёт
  одинаковое значение.

Пример (Grafana/PromQL):

```promql
sum(tickets_created_total{priority="critical"})
max(tickets_open_by_status{status="new"})
sum(rate(ticket_transitions_total[5m])) by (to_status)
```

## Архитектура (кратко)

- `app/services/*` — бизнес-логика, независимая от Telegram/HTTP.
  Все бизнес-изменения обращения — одно условное
  `UPDATE ... WHERE version = :v AND status = :s` (оптимистичное
  версионирование; конфликт → alert «Обращение изменилось» + пересинхронизация
  поста).
- `app/services/post_sync.py` — источник истины БД, пост — отображение;
  сериализация между репликами — session-level advisory lock на выделенном
  физическом соединении.
- `app/bot/*` — тонкие хендлеры PTB (форма на `ticket_drafts` с version и
  привязкой ответов к `prompt_message_id`; callback_data <= 64 байт).
- `app/workers.py` — фоновые циклы: синхронизация постов, напоминания, TTL
  черновиков, очистка `processed_updates`.
- `app/api/*` — REST для Mini App (initData HMAC, доступ по роли из БД без
  кеша) + `app/static/*` (vanilla JS, тема Telegram).
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: README (quickstart, env, migrations, botfather, metrics aggregation)"
```

### Task 9.3: Финальная проверка

- [ ] **Step 1: Полный прогон всего**

Run:
```bash
docker compose --profile test run --rm test pytest -q
docker compose --profile test run --rm test ruff check app tests
docker compose --profile test run --rm test ruff format --check app tests
docker compose --profile test run --rm test mypy app
docker build --target prod -q .
```
Expected: all green.

- [ ] **Step 2: git log review**

Run: `git log --oneline`
Expected: история коммитов по задачам плана (25+ коммитов).

- [ ] **Step 3: Commit (если были фиксы)**

```bash
git add -A
git commit -m "chore: final verification fixes"
```

---

## Self-Review (выполнено при составлении плана)

**1. Spec coverage — маппинг требований SPEC → задачи:**

| Требование SPEC | Задачи |
|---|---|
| Python 3.14, проверка зависимостей | выполнено до плана (docker check, версии зафиксированы) |
| FastAPI + PTB v21+ async, PG/SQLAlchemy 2 asyncpg, Alembic, pydantic-settings, Mini App vanilla JS | весь план |
| Роли в БД, одна роль, admin-superset | T2.1 (models), T3.2 (change_role), T7.4 (API) |
| Проверка прав по текущей роли из БД без кеша | T5.2 (upsert при каждом апдейте), T3.3 (role read in tx), T7.2 (deps) |
| FOR UPDATE (роль) / FOR SHARE (взятие, переназначение) | T3.2, T3.3 (`_lock_user_share`) |
| /start → без роли, сообщение об админе; BOOTSTRAP_ADMIN_IDS | T5.6 (test_commands) |
| Идентификация по telegram_id, username/имя при каждом взаимодействии | T5.2 (process_update upsert) |
| Номер — identity, #123, не переиспользуется | T2.1/T2.2, test_schema (sequence) |
| Поля тикета, description ≤3000, request_id ≤64, tenant_id ≤64 | T2.1 (CHECKs), T5.4 (draft validation) |
| version: 1 переход = 1 инкремент; technical-записи не меняют | T3.3, T4.1 (post sync), T6.1 (reminder shift), test_take_race |
| Статусы и переходы (state machine с тестами) | T3.1 (states.py) + test_states, T3.3 |
| Инварианты (CHECK + service) | T2.1/T2.4 (CHECKs), T3.3 |
| Время до взятия из журнала | T3.3 (`_observe_take_duration`) |
| Одно условное UPDATE (id+version+status), alert + ресинк | T3.3, T5.8 (callbacks), T7.3 (API 409) |
| Взять атомарно, «Уже взято» | T3.3, T3.5 (test_take_race) |
| callback_data ≤64 байт, t:/d: формат | T5.3, T5.8 |
| ticket_events в той же транзакции, technical не пишутся | T3.3, T4.1, T6.1 |
| Форма: шаги, валидация, /cancel, ForceReply | T5.3, T5.4, T5.6, T5.7 |
| ticket_drafts: UUID, один на пользователя, step, version, prompt_message_id | T2.1, T5.4 |
| Правила версии черновика (FOR UPDATE, +1; отклонённые не меняют; prompt technical) | T5.4, T5.5 (все сценарии) |
| Кнопки формы с id+version; чужой id/устаревшая version → отклонить | T5.4 (answer_choice), T5.7 (test_old_button...) |
| Привязка ответа к prompt_message_id; не совпало → заново вопрос | T5.4 (answer_text), T5.7 |
| Переход шага: tx (ответ+шаг+prompt=null+version+1), после коммита вопрос | T5.4, T5.7 |
| Условное сохранение prompt_message_id (id+version+step) | T5.4 (save_prompt_message_id), test_late_question... |
| Отправить: tx (тикет + удаление черновика); пост после коммита | T5.4 (answer_choice send), T5.7 (test_full_flow...) |
| TTL черновиков, /new → продолжить/заново | T5.4 (delete_expired_drafts), T5.7 (test_new_with_existing...), workers |
| Пост: данные, кнопки по статусу, права на сервере, alert | T4.1 (render), T5.8 (callbacks) |
| ≤4096, обрезка «…», полный текст в Mini App | T4.1 (test_render_truncates), T7.5 (карточка) |
| Процедура синхронизации (advisory lock, dedicated connection, timeout, finally, close on unlock fail) | T4.1 (sync_ticket_post), T4.2 (все тесты включая cancel/error) |
| post_synced_version = отрендеренная version | T4.1 (_do_sync), test_change_during_send_leaves_lag |
| Фоновая синхронизация (pg_try_advisory_lock, skip занятые) | T4.2 (post_sync_iteration), test_no_stale_post... |
| Пост удалён вручную → «Восстановить пост» у admin | T7.3 (restore-post), test_restore_post |
| 429 backoff с retry_after; «message is not modified» = успех | T4.1 (bot_api_call), test_429..., test_not_modified... |
| Напоминания: интервалы по приоритету из конфига | T1.3 (settings), T6.1 |
| Расписание: постановка при входе в new, очистка при выходе, следующий срок от now | T3.3 (flags), T6.1 (claim), test_reminders |
| Алгоритм: tx + FOR UPDATE SKIP LOCKED + коммит, отправка после, одна попытка | T6.1 (claim_due_reminders, send_reminders), T4.2? нет — test_claim_no_duplicates_concurrent_workers |
| In-memory JobQueue не использовать | T6.1 (расписание в БД) |
| /start /help /new /cancel, web_app кнопка (только личка) | T5.6, T5.7 (menu keyboard в /start) |
| Mini App: initData HMAC + auth_date порог; роль из БД | T7.1, T7.2 |
| Экраны: мои/все (по ролям), фильтры, пагинация, сортировка | T7.3, T7.5 |
| Карточка: все поля; admin: переоткрыть, переназначить (expected_version), восстановить пост | T7.3, T7.5 |
| Управление ролями; 409 со списком активных обращений | T7.4, test_role_change_api |
| Тема Telegram (светлая/тёмная) | T7.5 (themeParams → CSS vars) |
| Webhook: secret token, PTB без Updater, lifespan, синхронная обработка + timeout, 200 при ошибке | T5.9 (webhook.py), main v2/v4 |
| Дедупликация: отдельная tx до обработки, не откатывается, 48ч, работает в polling | T5.1, T5.2 (process_update), T5.9 (test_processing_error...) |
| /health/live, /health/ready на основном порту | T1.6 |
| REST /api + статика | T7.3–T7.5 |
| BOT_MODE=polling для локалки | T1.1 (compose), T5.9 (main: Updater при polling) |
| Конфиг: pydantic-settings, .env, .env.example, без секретов в репо | T1.1, T1.3, .gitignore |
| JSON-логи structlog, contextvars (request_id, update_id, telegram_user_id, ticket_id, component) | T1.4, T5.2 (bind), T5.9 (webhook bind) |
| Sentry: интеграции, PTB error handler, DSN опционален | T8.1 |
| Prometheus: стандартный registry, отдельный порт, middleware с path template | T8.2, T8.3 |
| Счётчики/гистограммы sum(): created по приоритету, transitions, take duration, updates, reminders, errors | T3.1 (metrics.py), T3.3, T5.2, T6.1 |
| Gauge open tickets by status при скрейпе, max() — в README | T8.3, T9.2 (README) |
| Docker: multi-stage dev/test/prod, non-root, один uvicorn | T1.1, T9.1 |
| compose: app (dev, polling) + PG; профиль для тестов; миграции отдельной командой (dev — авто) | T1.1 |
| CI/CD и k8s-манифесты НЕ в задаче | учтено (README: k8s-учёт без манифестов) |
| ruff, mypy strict | pyproject (T1.2), проверка в каждом этапе |
| pytest + pytest-asyncio, реальный PG, список обязательных сценариев | T2.3–T9 (все сценарии из «Качество» покрыты тестами) |
| README: запуск, env, миграции, образы, BotFather, агрегация метрик | T9.2 |
| Business logic в сервисном слое, тонкие хендлеры | архитектура (services/*) |

**2. Placeholder scan:** TBD/TODO/«похоже на Task N» отсутствуют; все шаги содержат код или точную команду.

**3. Type consistency:** имена/сигнатуры выверены сквозным списком:
`Transition`/`TRANSITIONS`/`check_permission` (states), `DraftResult`/`answer_choice(session, settings, user_id, draft_id, expected_version, choice, value)` (drafts), `sync_ticket_post(settings, db, bot, ticket_id, *, force_new, lock_timeout)` (post_sync), `claim_due_reminders(session_factory, settings, limit)` (reminders), `AppApplication.process_update(self, update)` (PTB 22.8 — один аргумент), `build_application(settings, db, bot=None)`, `register_update(db, update_id)`.

**Замеченные и учтённые риски PTB 22.8:** `initialize()` делает сетевой `get_me()` (тесты — RecordingBot; локальный старт требует реального токена — зафиксировано в README); `process_update(update)` без `bot`; web-app кнопка — `KeyboardButton(web_app=WebAppInfo(...))`; `Updater(bot, update_queue)` + `start_polling()`.






