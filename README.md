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
