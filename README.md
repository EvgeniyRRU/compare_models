# support-bot

Telegram-бот учёта обращений первой линии поддержки к дежурному разработчику.
Бот фиксирует обращения, ведёт статусы и исполнителя и показывает списки в Telegram Mini App.
График дежурств бот не ведёт: обращение берёт тот, кто нажал «Взять».

Подробная спецификация — в `SPEC.md`, правила для разработки — в `CLAUDE.md`.

## Стек

Python 3.14, FastAPI, python-telegram-bot 22, PostgreSQL, SQLAlchemy 2 (asyncpg), Alembic,
pydantic-settings, structlog, prometheus_client, sentry-sdk. Mini App — статика на vanilla JS.

## Локальный запуск

Нужны Docker и Docker Compose.

```bash
cp .env.example .env          # впишите BOT_TOKEN и DEV_CHAT_ID
docker compose up --build app # PostgreSQL, миграции и приложение с BOT_MODE=polling
```

- API и Mini App: http://localhost:8000 (Mini App — `/app/`)
- Метрики: http://localhost:9100/metrics
- Здоровье: `GET /health/live`, `GET /health/ready` (проверяет БД)

Без Docker: `uv sync --group dev`, поднять PostgreSQL, задать `DATABASE_URL` и остальные
переменные, затем `alembic upgrade head` и `uvicorn support_bot.main:app --reload`.

## Тесты

Тесты работают на настоящем PostgreSQL, не на SQLite и не на моках БД.

```bash
docker compose up -d db
docker compose run --rm test        # профиль test: весь набор pytest в контейнере
```

Локально (PostgreSQL из compose на порту 5433):

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

Тестовая база `support_bot_test` создаётся автоматически. Адрес задаётся `TEST_DATABASE_URL`.

## Переменные окружения

Полный список с комментариями — в `.env.example`. Важное:

| Переменная | Назначение |
| --- | --- |
| `DATABASE_URL` | URL PostgreSQL (`postgresql+asyncpg://…`) |
| `BOT_TOKEN` | Токен бота; также ключ проверки initData Mini App |
| `DEV_CHAT_ID` | Чат разработчиков: посты и напоминания |
| `BOT_MODE` | `polling` для разработки, `webhook` для production |
| `WEBHOOK_URL`, `WEBHOOK_SECRET_TOKEN` | Обязательны при `webhook` |
| `MINI_APP_URL` | HTTPS-адрес Mini App для кнопки меню |
| `BOOTSTRAP_ADMIN_IDS` | Telegram ID первых администраторов (получают роль на первом `/start`) |
| `SENTRY_DSN` | Пусто — Sentry отключён |
| `METRICS_PORT` | Порт `/metrics` (не основной HTTP-порт) |

Секреты в репозиторий не коммитятся. В production они приходят из внешнего пайплайна как env.

## Миграции

Миграции — отдельная команда, при старте приложения не запускаются:

```bash
alembic upgrade head        # локально или отдельным job в Kubernetes
alembic revision --autogenerate -m "описание"   # новая миграция после правок моделей
```

Для dev в compose миграции запускаются автоматически перед приложением.

## Сборка образов

Один Dockerfile, три таргета:

```bash
docker build --target prod -t support-bot:prod .   # минимальный образ, non-root, без dev-зависимостей
docker build --target dev  -t support-bot:dev .    # dev-зависимости, hot reload
docker build --target test -t support-bot:test .   # dev-зависимости, по умолчанию pytest
```

Prod запускает ровно один процесс uvicorn (без `--workers` и gunicorn) и открывает порты
8000 (HTTP) и 9100 (метрики). Масштабирование — только числом реплик.

## Регистрация в BotFather и Telegram

1. Создайте бота, получите токен → `BOT_TOKEN`.
2. Добавьте бота в чат разработчиков и выдайте права на отправку сообщений → `DEV_CHAT_ID`.
   Узнать id чата можно через `getUpdates` после отправки сообщения в чат.
3. Mini App: `/newapp` в BotFather для бота, URL = публичный HTTPS-адрес `…/app/`.
   Тот же адрес передайте в `MINI_APP_URL`, чтобы появилась кнопка меню.
   Кнопки web_app работают только в личных чатах.
4. Webhook (production): `BOT_MODE=webhook`, `WEBHOOK_URL=https://<host>/telegram/webhook`,
   `WEBHOOK_SECRET_TOKEN` — случайная строка. Приложение регистрирует webhook при старте
   и проверяет заголовок `X-Telegram-Bot-Api-Secret-Token` у каждого апдейта.

## Метрики и правила агрегации

Все метрики отдаются на `METRICS_PORT`, эндпоинт `/metrics`.

- Счётчики и гистограммы (`tickets_created_total`, `ticket_transitions_total`,
  `ticket_time_to_take_seconds`, `updates_processed_total`, `updates_duplicate_total`,
  `reminders_sent_total`, `errors_total`, `http_*`) суммируются по подам через `sum()`.
- Gauge `tickets_open{status}` вычисляется запросом к БД при каждом скрейпе. Каждая реплика
  отдаёт одно и то же значение, поэтому агрегируйте его через `max()`, а не `sum()`.
  Например: `max by (status) (tickets_open)`.

## Структура

```
src/support_bot/
  config.py          настройки (pydantic-settings)
  db/models.py       модели SQLAlchemy и CHECK-ограничения
  domain/            state machine, роли, ошибки (без Telegram и HTTP)
  services/          бизнес-логика: обращения, черновики, синхронизация поста, напоминания
  bot/               PTB: тонкие хендлеры и формулировки вопросов формы
  web/               FastAPI: webhook, health, REST API для Mini App, проверка initData
  static/            Mini App (vanilla JS, без сборки)
  workers.py         фоновые задачи: синхронизация постов, напоминания, очистка
  main.py            lifespan и точка входа
migrations/          Alembic
tests/               pytest на PostgreSQL
```
