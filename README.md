# Support bot

Telegram-бот учёта обращений первой линии поддержки к дежурному разработчику + Mini App.
Бот фиксирует обращения, статусы и исполнителя; общение по сути обращения идёт вне бота.
Подробная спецификация: [SPEC.md](SPEC.md).

Стек: Python 3.14, FastAPI, python-telegram-bot 22, PostgreSQL, SQLAlchemy 2 (async) + Alembic, structlog, Sentry, Prometheus.

## Локальный запуск

Нужны Docker и Python 3.14 (для работы без Docker — `uv sync --group dev`).

```bash
cp .env.example .env          # заполнить BOT_TOKEN, DEV_CHAT_ID, BOOTSTRAP_ADMIN_IDS
docker compose up --build     # app (BOT_MODE=polling, hot reload, автомиграции) + PostgreSQL
```

- API и Mini App: http://localhost:8000 (`/app/`), метрики: http://localhost:9100/metrics.
- Публичный HTTPS для локальной разработки не нужен: в режиме `polling` бот сам забирает апдейты.
  Кнопка Mini App в Telegram всё равно требует HTTPS URL (`WEBAPP_URL`), для её проверки нужен туннель.
- Первый админ: укажите свой Telegram ID в `BOOTSTRAP_ADMIN_IDS` и отправьте боту `/start`.

Без Docker для приложения (БД из compose):

```bash
docker compose up -d db
uv sync --group dev
export DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/support_bot
alembic upgrade head
uvicorn app.main:create_app --factory --reload --reload-dir src --app-dir src
```

## Переменные окружения

Все переменные с комментариями — в [.env.example](.env.example). Обязательны `BOT_TOKEN` и `DEV_CHAT_ID`.
Секреты в репозитории не хранятся: в production приходят из внешнего пайплайна как env, локально — из `.env`.

## Миграции

Миграции Alembic запускаются отдельной командой и **не** при старте приложения
(в compose для dev автозапуск включён):

```bash
alembic upgrade head          # в k8s — отдельный job на том же образе
alembic revision --autogenerate -m "описание"
```

## Тесты и качество

Тесты идут на реальном PostgreSQL (база `support_bot_test` создаётся автоматически, схема — миграциями).

```bash
docker compose up -d db
pytest                                  # все тесты (TEST_DATABASE_URL по умолчанию localhost:5433)
pytest tests/test_tickets.py::test_take_race_only_one_wins   # один тест
docker compose --profile test run --rm tests                 # то же в контейнере
ruff check . && ruff format --check .
mypy
```

## Сборка образов

Один Dockerfile, три таргета:

```bash
docker build --target dev  -t support-bot:dev .
docker build --target test -t support-bot:test .   # по умолчанию запускает pytest
docker build --target prod -t support-bot:prod .   # non-root, один uvicorn-процесс, порты 8000 и 9100
```

Масштабирование — только числом реплик (в pod один процесс). `/health/live` и `/health/ready` — на основном порту.

## Webhook и Mini App в BotFather

1. В production: `BOT_MODE=webhook`, `WEBHOOK_URL=https://<host>/telegram/webhook`, `WEBHOOK_SECRET=<случайная строка>`.
   Приложение само вызывает `setWebhook` при старте с `secret_token`; запросы без заголовка
   `X-Telegram-Bot-Api-Secret-Token` отклоняются. HTTPS настраивают администраторы инфраструктуры.
2. Mini App: в @BotFather → `/mybots` → бот → *Bot Settings* → *Menu Button* → задать URL `https://<host>/app/`
   (или `/newapp`). Тот же URL укажите в `WEBAPP_URL` — приложение выставит кнопку меню само.
3. Добавьте бота в чат разработчиков (`DEV_CHAT_ID`) с правом писать сообщения.
4. `/setcommands`: `new` — создать обращение, `cancel` — отмена, `help` — справка.

## Метрики и правила агрегации

`/metrics` отдаётся на отдельном порту `METRICS_PORT` (по умолчанию 9100) — не публикуйте его через ingress.
Используется стандартный registry (один процесс на pod, multiprocess-режим не нужен).

- Счётчики и гистограммы (`tickets_created_total`, `ticket_transitions_total`, `ticket_time_to_take_seconds`,
  `telegram_updates_total`, `reminders_sent_total`, `errors_total`, `http_*`) считаются в процессе —
  по подам их нужно **суммировать**: `sum(rate(...))`, `sum by (le) (...)`.
- Gauge `tickets_open{status=...}` вычисляется запросом к БД при каждом скрейпе, поэтому каждая реплика
  отдаёт одинаковое значение — агрегировать его нужно через **`max()`**, а не `sum()`:
  `max by (status) (tickets_open)`.

## Архитектурные заметки

- Приложение работает в нескольких репликах: состояние формы создания обращения (`ticket_drafts`),
  дедупликация апдейтов (`processed_updates`), расписание напоминаний (`tickets.next_reminder_at`)
  и блокировки синхронизации поста (PostgreSQL advisory locks) хранятся в БД.
- Бизнес-изменения обращения — один условный `UPDATE ... WHERE version = :v AND status = :s` с журналом
  в `ticket_events`; технические записи (`message_id`, `post_synced_version`, перенос напоминания) `version` не меняют.
- Пост в чате разработчиков — отображение БД; сбой публикации подхватывает фоновая синхронизация.
  Если пост удалили вручную после синхронизации, используйте «Восстановить пост» в карточке Mini App (admin).
- Напоминания — не более одной попытки отправки на срок; потеря допустима.
