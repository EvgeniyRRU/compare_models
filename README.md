# Rent Support Bot

Telegram-бот дежурного разработчика для обработки обращений поддержки аренды:
анкета создания обращения, lifecycle-кнопки на постах в дев-чате, напоминания о
невзятых обращениях, Mini App для просмотра/управления, REST API и метрики Prometheus.

## Архитектура

- **Бот** (`app/bot/`) — приём апдейтов через webhook (`POST /telegram/webhook`,
  проверка `X-Telegram-Bot-Api-Secret-Token`, всегда 200) или long polling
  (`BOT_MODE=polling`). Дедупликация по `processed_updates`, таймаут обработки
  одного апдейта, команды `/start /help /new /cancel`, пошаговая анкета создания
  обращения (`TicketDraft`), inline-кнопки действий над обращением.
- **State machine** (`app/domain/state_machine.py`) — действия: взять, в работу,
  ожидает информации, решено/отклонено/дубль, отказаться, переоткрыть,
  переназначить. Оптимистичная блокировка по `version` (ошибка
  «Обращение изменилось»), правила ролей (`support / developer / admin`).
- **Посты в дев-чате** (`app/bot/render.py`, `app/services/post_sync.py`) —
  карточка обращения ≤4096 символов, синхронизация поста под advisory-lock на
  обращение; чередующий sweeper чинит рассинхрон (`post_synced_version`).
- **REST API + Mini App** (`app/api/`, `app/static/mini_app/`) — авторизация по
  `initData` (HMAC-SHA256, `WebAppData`), выдача роли из БД на каждый запрос;
  экраны «Мои/Все», фильтры, карточка, админ-действия (reopen, reassign,
  «Восстановить пост»), экран ролей. Раздаётся на `/miniapp`.
- **Воркеры** (`app/workers/`) — post-sync sweeper, напоминания
  (`FOR UPDATE SKIP LOCKED`, перенос следующего напоминания от момента
  клейма — без «догоняющего шторма»), cleanup черновиков и dedupe-таблицы.
- **Мониторинг** — `/metrics` на отдельном порту `METRICS_PORT` (9090),
  middleware HTTP-метрик, structlog JSON-логи, опциональный Sentry.

Стек: Python 3.14, aiogram-альтернатива — python-telegram-bot 22, FastAPI,
SQLAlchemy 2 (asyncpg), Alembic, Prometheus client. Всё разработческое
окружение — в Docker.

## Локальная разработка (Docker)

Требуется только Docker. Все команды — через compose.

```bash
cp .env.example .env            # заполните BOT_TOKEN, DEV_CHAT_ID, BOOTSTRAP_ADMIN_IDS

docker compose --profile dev up -d postgres app
# app: сам применяет alembic upgrade head и запускает uvicorn --reload
# бот в режиме polling (BOT_MODE=polling в compose) — публичный HTTPS не нужен
```

Тесты / линт / типы (контейнер `test`, БД `rent_bot_test` создаётся init-скриптом):

```bash
docker compose run --rm test pytest tests/ -q
docker compose run --rm test ruff check app tests
docker compose run --rm test mypy app
```

PostgreSQL для ручных проверок: `localhost:5433`, пользователь `rent/rent`,
БД `rent_bot` и `rent_bot_test`.

## Переменные окружения

| Переменная | По умолчанию | Описание |
|---|---|---|
| `BOT_TOKEN` | — (обязательно) | Токен бота от BotFather |
| `BOT_MODE` | `polling` | `polling` или `webhook` |
| `WEBHOOK_SECRET_TOKEN` | пусто | Секрет в заголовке `X-Telegram-Bot-Api-Secret-Token`; пусто = не проверять |
| `DEV_CHAT_ID` | — (обязательно) | Chat id дев-чата, куда публикуются посты |
| `BOOTSTRAP_ADMIN_IDS` | пусто | Telegram id, получающие роль `admin` при первом `/start` (список через запятую) |
| `DATABASE_URL` | `postgresql+asyncpg://rent:rent@localhost:5432/rent_bot` | DSN PostgreSQL (драйвер asyncpg) |
| `PORT` | `8000` | HTTP-порт приложения (webhook, API, /health, /miniapp) |
| `METRICS_PORT` | `9090` | Порт Prometheus-метрик (отдельный сервер) |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `json` | Уровень; `json` (prod) или `console` (dev) |
| `AUTH_DATE_MAX_AGE_SECONDS` | `3600` | Свежесть `auth_date` initData Mini App |
| `WEB_APP_URL` | пусто | Публичный https URL Mini App; пусто — кнопка меню не ставится |
| `PROCESS_UPDATE_TIMEOUT_SECONDS` | `30` | Таймаут синхронной обработки одного апдейта |
| `POST_SYNC_INTERVAL_SECONDS` | `30` | Интервал post-sync sweepera |
| `REMINDER_POLL_INTERVAL_SECONDS` | `30` | Интервал проверки напоминаний |
| `ADVISORY_LOCK_TIMEOUT_SECONDS` | `10` | Ожидание advisory-lock на обращение |
| `TG_RETRY_ATTEMPTS` | `5` | Попыток Telegram API с backoff (учитывает 429 retry_after) |
| `DRAFT_TTL_HOURS` | `24` | TTL незавершённых анкет |
| `PROCESSED_UPDATES_TTL_HOURS` | `48` | Хранение дедуп-таблицы |
| `REMINDER_INTERVAL_CRITICAL_MIN` | `10` | Интервал напоминаний, критический приоритет |
| `REMINDER_INTERVAL_HIGH_MIN` | `30` | …высокий |
| `REMINDER_INTERVAL_MEDIUM_MIN` | `120` | …средний |
| `REMINDER_INTERVAL_LOW_MIN` | `480` | …низкий |
| `SENTRY_DSN` | пусто | Пусто — Sentry выключен |

## Миграции

Dev: применяются автоматически при старте сервиса `app` в compose.

Prod — отдельным job **до** раскатки новых контейнеров:

```bash
docker compose --profile prod run --rm migrate   # alembic upgrade head
```

Или напрямую: `alembic -x url=$DATABASE_URL upgrade head`.

## Продакшен-образ и запуск

```bash
docker build --target prod -t rent-support-bot .
# или через compose:
docker compose --profile prod up -d app-prod
```

Образ: `python:3.14-slim`, только `app/` + `alembic.ini`, non-root
(`appuser`, uid 10001), один процесс uvicorn. Порты: `8000` (HTTP) и `9090`
(метрики). Масштабирование — репликами (`docker compose ... up -d --scale
app-prod=3`), горизонтальная работа безопасна: блокировки (FOR UPDATE /
SKIP LOCKED / advisory-локи) — в PostgreSQL. Секреты — из внешнего
контура env (в репозиторий `.env` не коммитится).

Webhook-режим требует публичного HTTPS URL приложения.

## Настройка Telegram (BotFather)

1. **Команды** (`/setcommands`) — приложение само вызывает `set_my_commands`,
   но в BotFather можно продублировать:
   ```
   new - Создать обращение
   cancel - Отменить анкету
   help - Справка
   start - Начать
   ```
2. **Webhook** (для `BOT_MODE=webhook`) — c секретом:
   ```bash
   curl -F "url=https://bot.example.com/telegram/webhook" \
        -F "secret_token=<WEBH…OKEN>" \
        -F "allowed_updates=[\"message\",\"edited_message\",\"callback_query\"]" \
        "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook"
   ```
   Без `secret_token` бот примет любые запросы на endpoint — в проде секрет
   обязателен.
3. **Mini App**: BotFather → `/newapp` (или Settings → Menu Button) → указать
   `https://bot.example.com/miniapp/`; затем в env — `WEB_APP_URL`. Кнопка
   «Обращения» в меню чата ставится автоматически при непустом `WEB_APP_URL`.
   Backend проверяет подпись `initData` (HMAC-SHA256 с ключом
   `HMAC_SHA256("WebAppData", BOT_TOKEN)`) и свежесть `auth_date`.

## Метрики (`:9090/metrics`)

- `tickets_created_total{priority}` — создание обращений
- `ticket_transitions_total{from_status,to_status}` — смена статусов
- `ticket_time_to_take_seconds{priority}` (histogram) — время до взятия в работу
- `updates_processed_total` / `updates_deduplicated_total` — приём апдейтов
- `reminders_sent_total{priority}` — отправленные напоминания
- `post_sync_total{result}` — `sent|edited|republished|skipped|gone|error`
- `app_errors_total{component}` — ложные/необработанные ошибки
- `http_requests_total{method,route,status}` и `http_request_duration_seconds`
  (route — шаблон пути, у статики/неизвестных — `unmatched`)
- `open_tickets_by_status{status}` — gauge: открытые (не финальные) обращения

**Правила агрегации для алертов/дашбордов:** счётчики по всем репликам
суммировать — `sum(rate(...))`; gauge `open_tickets_by_status` — **`max()`**,
не `sum()`: каждая реплика читает одно и то же значение из БД, суммирование
завысит его в число раз реплик. Histogram — обычно `histogram_quantile` по
`sum(rate(..._bucket[5m]))`.

## Health

- `GET /health/live` — процесс жив
- `GET /health/ready` — 200, если PostgreSQL отвечает (иначе 503)
- `GET /healthz` — на порту метрик
