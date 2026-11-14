# Support Bot

Telegram-бот и Mini App для учёта обращений первой линии поддержки к дежурному
разработчику. Бот фиксирует обращения, публикует карточку в чате разработчиков,
ведёт статусы, исполнителя и журнал изменений. Графиком дежурств бот не
управляет: обращение берёт тот разработчик, который нажал «Взять».

## Возможности

- Создание обращения в личке с ботом: описание, приоритет, ID заявки,
  ID арендатора (опционально), подтверждение.
- Пост-карточка в чате разработчиков с кнопками смены статуса.
- Mini App: «Мои обращения», «Все обращения», фильтры, пагинация, карточка,
  админские действия и управление ролями.
- Роли `support`, `developer`, `admin`; права проверяются по БД на каждый
  апдейт/запрос.
- Напоминания о невзятых обращениях с интервалом по приоритету.
- Надёжная дедупликация апдейтов, защита от гонок через `version`.
- JSON-логи, Sentry (опционально), Prometheus-метрики на отдельном порту.

## Статусы и переходы

`new → in_progress` — «Взять» (developer/admin).
`in_progress ↔ waiting_info` — исполнитель или admin.
`in_progress | waiting_info → resolved | rejected | duplicate` — исполнитель или admin.
`in_progress | waiting_info → new` — «Отказаться», исполнитель или admin.
`resolved | rejected | duplicate → new` — переоткрытие, только admin.
Переназначение — только admin, в статусах `in_progress` и `waiting_info`.

Из `new` нельзя сразу закрыть обращение: сначала его берут.

## Быстрый старт в Docker

```bash
cp .env.example .env
# заполните BOT_TOKEN, DEV_CHAT_ID, BOOTSTRAP_ADMIN_IDS
docker compose up --build
```

В dev-режиме compose сам применяет миграции и запускает uvicorn с hot reload.
Приложение работает в режиме long polling, публичный HTTPS не нужен.
PostgreSQL доступен на `localhost:5433`.

Открыть приложение: `http://localhost:8000/` (вне Telegram Mini App не
аутентифицируется — это нормально).

## Локальный запуск без Docker

```bash
uv sync
# PostgreSQL должен быть доступен, DATABASE_URL указывает на него
alembic upgrade head
BOT_MODE=polling uvicorn support_bot.main:app --reload --port 8000
```

Проверки:

```bash
ruff check .
ruff format --check .
mypy
pytest
```

## Переменные окружения

Все переменные и комментарии — в `.env.example`. Кратко:

| Переменная | Назначение |
|---|---|
| `BOT_TOKEN` | токен бота, обязателен |
| `BOT_MODE` | `polling` (dev) или `webhook` (prod) |
| `TELEGRAM_WEBHOOK_SECRET` | секрет заголовка `X-Telegram-Bot-Api-Secret-Token`, обязателен в webhook |
| `DEV_CHAT_ID` | чат разработчиков |
| `BOOTSTRAP_ADMIN_IDS` | Telegram ID первичных админов через запятую |
| `DATABASE_URL` | `postgresql+asyncpg://...` |
| `MINI_APP_URL` | публичный HTTPS URL Mini App, включает кнопку меню |
| `INIT_DATA_MAX_AGE_SECONDS` | максимальный возраст `initData` |
| `REMINDER_*_MINUTES` | интервалы напоминаний по приоритетам |
| `REMINDER_POLL_SECONDS`, `REMINDER_BATCH_SIZE` | опрос напоминаний |
| `POST_SYNC_POLL_SECONDS`, `POST_SYNC_LOCK_TIMEOUT_SECONDS` | фоновая синхронизация постов |
| `DRAFT_TTL_HOURS` | TTL черновика формы (по умолчанию 24 ч) |
| `UPDATES_RETENTION_HOURS` | хранение `processed_updates` (48 ч) |
| `WEBHOOK_TIMEOUT_SECONDS` | таймаут обработки апдейта |
| `WORKERS_ENABLED` | фоновые задачи в процессе |
| `LOG_LEVEL`, `LOG_FORMAT` | `json` для production/OpenSearch, `console` для dev |
| `SENTRY_DSN`, `SENTRY_ENVIRONMENT`, `SENTRY_TRACES_SAMPLE_RATE` | Sentry, DSN опционален |
| `METRICS_ENABLED`, `METRICS_PORT` | Prometheus на отдельном порту (9100) |

Секретов в репозитории нет; в production они приходят из внешнего пайплайна.
`BOOTSTRAP_ADMIN_IDS` срабатывает при первом `/start` пользователя.

## Миграции

```bash
alembic upgrade head
alembic revision --autogenerate -m "change"
```

В Kubernetes миграции запускаются отдельным job, а не при старте приложения.
В dev compose автозапуск допустим.

## Telegram: webhook и Mini App

1. Создайте бота у [@BotFather](https://t.me/BotFather) и получите токен.
2. В production запустите приложение с `BOT_MODE=webhook` и
   `TELEGRAM_WEBHOOK_SECRET=<секрет>`.
3. Зарегистрируйте webhook (HTTPS и ingress настраивает инфраструктура):

```bash
curl -F "url=https://example.com/telegram/webhook" \
     -F "secret_token=$TELEGRAM_WEBHOOK_SECRET" \
     "https://api.telegram.org/bot$BOT_TOKEN/setWebhook"
```

4. Mini App: статика отдаётся тем же FastAPI; укажите
   `MINI_APP_URL=https://example.com/app`. В BotFather создайте Mini App
   (`/newapp`) с этим URL; кнопка меню в личных чатах ставится ботом при
   `/start`, если `MINI_APP_URL` задан. Web App кнопки работают только в
   личных чатах.
5. Для локальной отладки Mini App нужен HTTPS-туннель (например, ngrok),
   потому что Telegram не открывает Web App по HTTP.

## Docker-образы

Один multi-stage `Dockerfile`:

- `dev` — dev-зависимости, hot reload, монтирование исходников.
- `test` — тестовые зависимости, по умолчанию `pytest`.
- `prod` — без dev-зависимостей, non-root `appuser`, один uvicorn-процесс,
  порты 8000 (HTTP) и 9100 (метрики).

```bash
docker build --target prod -t support-bot:prod .
docker compose --profile test run --rm --build test
```

## Тесты

Тесты используют реальный PostgreSQL. Локально:

```bash
docker compose up -d db
DATABASE_URL=postgresql+asyncpg://support:support@localhost:5433/support_test pytest
```

Или в контейнере:

```bash
docker compose --profile test run --rm --build test
```

Conftest создаёт `support_test`, применяет Alembic и очищает таблицы между
тестами.

## Метрики

`/metrics` отдаётся только на `METRICS_PORT` (по умолчанию 9100), чтобы не
попадать наружу через ingress. В каждом pod один uvicorn-процесс, поэтому
используется стандартный registry без multiprocess-режима.

Правила агрегации в Prometheus:

- счётчики и гистограммы (`support_bot_tickets_created_total`,
  `support_bot_ticket_transitions_total`,
  `support_bot_ticket_time_to_take_seconds`,
  `support_bot_http_requests_total` и т.д.) — `sum()`;
- gauge `support_bot_open_tickets{status="..."}` считается запросом к БД при
  скрейпе и одинаков во всех репликах — агрегировать через `max()`, не `sum()`.

Маршрут в HTTP-метриках — шаблон пути (`/api/tickets/{ticket_id}`), а не
фактический URL, чтобы не раздувать кардинальность.

## Как это устроено

- Бизнес-логика — в `src/support_bot/services`, не зависит от Telegram/HTTP.
- Все бизнес-изменения обращения — один условный `UPDATE` с проверкой
  `id + version + status` и инкрементом `version`; журнал `ticket_events`
  пишется в той же транзакции.
- Форма создания хранится в `ticket_drafts` (одна на пользователя), шаги
  защищены `version`; текстовый ответ принимается только как reply на
  текущий вопрос (`prompt_message_id`).
- Синхронизация поста сериализуется PostgreSQL advisory lock на выделенном
  физическом соединении; в БД сохраняется версия, из которой отрендерен пост.
  Если состояние изменилось во время отправки, фоновая задача синхронизирует
  пост ещё раз.
- Напоминания и фоновая синхронизация постов используют БД как расписание
  (`SKIP LOCKED` / try-lock), поэтому работают в нескольких репликах.
- Апдейты дедуплицируются записью `update_id` в `processed_updates` отдельной
  транзакцией до бизнес-обработки; ошибка обработки её не откатывает.

## Структура

```
src/support_bot/
  api/          FastAPI-роуты, initData-авторизация, webhook
  bot/          PTB Application, handlers, callback-кодирование
  db/           модели, сессии, advisory lock
  domain/       state machine, ошибки, политика напоминаний
  integrations/ Telegram-публикатор постов
  observability/ structlog, Sentry, Prometheus
  services/     бизнес-логика
  workers/      фоновые циклы
  web/static/   Mini App без сборки (vanilla JS)
migrations/     Alembic
tests/          pytest + реальный PostgreSQL
```