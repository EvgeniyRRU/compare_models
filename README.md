# Бот учёта обращений

Telegram-бот и Mini App для учёта обращений к дежурным разработчикам. Источник истины — PostgreSQL. Обращения и черновики защищены версиями; фоновые задачи можно запускать в нескольких репликах. Интерфейс — на русском языке.

## Локальный запуск

Нужны Docker и Docker Compose. Создайте `.env` из `.env.example`, заполните `BOT_TOKEN`, `DEV_CHAT_ID`, `MINI_APP_URL`, `WEBHOOK_SECRET` и `BOOTSTRAP_ADMIN_IDS`. Список администраторов указывается в JSON, например `[12345678,87654321]`. Первичный администратор должен отправить боту `/start`. Бот должен иметь возможность писать и редактировать сообщения в чате разработчиков.

```sh
cp .env.example .env
docker compose up -d db
docker compose run --rm app alembic upgrade head
docker compose up app
```

Локальный режим использует polling, удаляя настроенный webhook. Одновременно запускайте только один экземпляр polling для данного токена. Для работы Mini App Telegram требует публичный HTTPS-адрес; укажите его в `MINI_APP_URL`. В контейнере доступ к PostgreSQL задаётся через `db:5432`; Compose передаёт это значение как `DATABASE_URL`.

Миграции запускаются отдельной командой, не при старте приложения. В production используйте отдельный job с той же версией образа и командой `alembic upgrade head`. Перед обновлением сделайте резервную копию базы.

## Тесты и проверки

```sh
docker compose --profile test run --build --rm test
docker compose --profile test run --rm test ruff check app tests alembic
docker compose --profile test run --rm test ruff format --check app tests alembic
docker compose --profile test run --rm test mypy app
```

Тестовый сервис использует отдельную базу `db_test`; тесты пересоздают её таблицы. Не указывайте в `TEST_DATABASE_URL` базу с нужными данными.

## Образы

```sh
docker build --target dev -t support-bot:dev .
docker build --target test -t support-bot:test .
docker build --target prod -t support-bot:prod .
```

Production запускает один процесс uvicorn под непривилегированным пользователем. Основной порт — 8000, отдельный порт метрик — `METRICS_PORT` (9090 по умолчанию). Масштабирование — числом реплик, не процессами uvicorn.

## Telegram

В BotFather создайте бота, задайте URL Mini App через настройки бота и настройте кнопку меню. Адрес должен использовать HTTPS. Добавьте бота в чат разработчиков и предоставьте ему необходимые права. Откройте бота в личном чате и отправьте `/start`; после получения роли доступны `/new`, `/cancel`, `/help` и кнопка Mini App.

Для production задайте `BOT_MODE=webhook`, опубликуйте HTTPS-маршрут `/telegram/webhook` и зарегистрируйте webhook через Telegram Bot API, передав в `setWebhook` параметры `url=https://ваш-домен/telegram/webhook` и `secret_token` со значением `WEBHOOK_SECRET`. Секрет и токен храните в системе секретов, а не в репозитории. При возврате к polling webhook удаляется при старте.

## Настройки

Полный список с примерами находится в `.env.example`:

| Переменные | Назначение |
|---|---|
| `BOT_TOKEN`, `WEBHOOK_SECRET` | Токен Bot API и секрет webhook |
| `DATABASE_URL` | SQLAlchemy URL с драйвером `postgresql+asyncpg` |
| `DEV_CHAT_ID`, `MINI_APP_URL`, `BOT_MODE` | Чат разработчиков, HTTPS-адрес Mini App и режим `polling`/`webhook` |
| `BOOTSTRAP_ADMIN_IDS` | JSON-массив Telegram ID первичных администраторов |
| `DRAFT_TTL_HOURS`, `INIT_DATA_MAX_AGE_SECONDS` | Срок хранения черновиков и допустимый возраст авторизации |
| `WEBHOOK_TIMEOUT_SECONDS` | Таймаут обработки одного обновления |
| `REMINDER_POLL_SECONDS`, `SYNC_POLL_SECONDS` | Интервалы опроса фоновых задач |
| `REMINDER_*_MINUTES` | Интервалы напоминаний для каждого приоритета |
| `METRICS_PORT`, `SENTRY_DSN` | Порт метрик и необязательный DSN Sentry |
| `LOG_LEVEL`, `LOG_FORMAT` | Уровень и формат логов: `json` или `console` |

Секреты в production передаются через переменные окружения из внешней системы. `/health/live` проверяет процесс, `/health/ready` — доступность БД.

## Метрики

Scrape отдельного порта метрик. Счётчики и гистограммы собираются в каждом процессе; агрегируйте их по pod с `sum()`, например `sum(rate(tickets_created_total[5m])) by (priority)`. `tickets_open` вычисляется запросом к общей базе и одинаков на всех репликах: используйте **`max()`**, например `max(tickets_open) by (status)`, иначе обращения будут посчитаны несколько раз. Лейбл HTTP-маршрута содержит шаблон пути, а не идентификатор обращения.

Напоминания отправляются максимум один раз на срок, после сдвига расписания в БД; при сбое отправки этот срок не повторяется. При удалении синхронизированного поста вручную администратор может восстановить его в карточке обращения.
