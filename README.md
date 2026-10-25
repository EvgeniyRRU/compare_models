# Бот учёта обращений к дежурному разработчику

Telegram-бот и Mini App для учёта обращений первой линии поддержки к дежурному
разработчику. Бот фиксирует обращения, публикует их в чат разработчиков, отслеживает
статус и исполнителя, напоминает о невзятых обращениях. Графиком дежурств бот не
управляет: обращение берёт тот, кто нажал «Взять». Обсуждение по сути идёт вне бота.

Стек: Python 3.14, FastAPI, python-telegram-bot 22 (async, без Updater), PostgreSQL,
SQLAlchemy 2 (asyncpg), Alembic, pydantic-settings, structlog, Sentry, Prometheus.
Mini App написан на vanilla JS и Telegram WebApp SDK, без сборки.

## Как это устроено

- **Источник истины — PostgreSQL.** Состояние формы, расписание напоминаний и
  дедупликация апдейтов хранятся в БД, поэтому приложение работает в нескольких
  репликах, и соседние апдейты одного пользователя могут попасть в разные поды.
- **Обращения.** State machine с явными переходами (`domain/state_machine.py`). Каждое
  бизнес-изменение выполняется одним условным
  `UPDATE ... WHERE id AND version AND status`, которое увеличивает `version` и пишет
  событие в `ticket_events` в той же транзакции. Кнопки поста и запросы Mini App
  передают ожидаемую `version`. При расхождении пользователь получает alert
  «Обращение изменилось» или «Уже взято».
- **Пост в чате разработчиков** — отображение БД. Одна процедура синхронизации
  выполняется под session-level advisory lock по id обращения. Она читает актуальное
  состояние, отправляет или редактирует пост и сохраняет отрендеренную версию в
  `post_synced_version`. Фоновая задача в каждой реплике досинхронизирует отставшие
  посты.
- **Напоминания.** `next_reminder_at` хранится в БД. Воркеры выбирают сработавшие
  сроки через `FOR UPDATE SKIP LOCKED`, сдвигают их и только после коммита отправляют
  напоминание: не больше одной попытки на каждый срок.
- **Апдейты.** `update_id` коммитится в `processed_updates` отдельной транзакцией до
  обработки, поэтому каждый апдейт обрабатывается не больше одного раза. Webhook и
  polling используют один конвейер (`telegram/pipeline.py`).
- Бизнес-логика находится в `services/` и не зависит от Telegram и HTTP. Хендлеры бота
  (`telegram/handlers.py`) и эндпоинты (`api/routes.py`) остаются тонкими.

```
src/support_bot/
  config.py            настройки (pydantic-settings)
  main.py              FastAPI + lifespan (PTB, воркеры, сервер метрик)
  domain/              enums, state machine, ошибки, валидация — без I/O
  db/                  модели, engine, advisory lock
  services/            бизнес-слой: users, tickets, drafts, post_sync, reminders, …
  telegram/            хендлеры, форма, publisher с ретраями, pipeline, polling
  api/                 initData-авторизация, REST API /api, webhook, health
  workers/             фоновые циклы
  monitoring/          метрики, collector, middleware, Sentry
  webapp/              статика Mini App
migrations/            Alembic
tests/                 pytest на реальном PostgreSQL
```

## Роли

| Роль        | Права |
|-------------|-------|
| нет роли    | Только регистрация через /start. Доступа нет |
| `support`   | Создание обращений, просмотр |
| `developer` | Взятие обращений, смена статусов своих обращений, просмотр |
| `admin`     | Всё, что могут support и developer, а также любые статусы, переоткрытие, переназначение, «Восстановить пост», управление ролями |

Права проверяются по текущей роли из БД на каждый апдейт и каждый запрос. Роль
developer или admin нельзя заменить ролью без права быть исполнителем, пока у
пользователя есть обращения в `in_progress` или `waiting_info`: API вернёт 409 со
списком этих обращений. Нельзя менять роль самому себе и снимать роль с последнего
администратора. Первичные админы задаются в `BOOTSTRAP_ADMIN_IDS` и получают роль при
`/start`, если роли у них ещё нет.

## Локальный запуск

Нужны Docker и [uv](https://docs.astral.sh/uv/).

1. Создайте бота у [@BotFather](https://t.me/BotFather) и тестовый чат разработчиков.
   Добавьте бота в чат. Id чата можно узнать, например, через `getUpdates`.
2. Подготовьте конфигурацию:
   ```bash
   cp .env.example .env
   # заполните BOT_TOKEN, DEV_CHAT_ID, BOOTSTRAP_ADMIN_IDS (ваш Telegram ID)
   ```
3. Запустите приложение и PostgreSQL:
   ```bash
   docker compose up --build
   ```
   Сервис `app` собирается из таргета `dev`, работает в `BOT_MODE=polling` (публичный
   HTTPS не нужен), сам применяет миграции и перезапускается при изменении `src/`.
   Порты: `8000` для HTTP и `9000` для `/metrics`.
4. Напишите боту `/start`, затем `/new`.

Mini App требует публичного HTTPS-адреса. Для локальной проверки можно поднять туннель
(ngrok, cloudflared) на порт 8000, указать `WEBAPP_URL=https://<туннель>/app/` и
зарегистрировать адрес в BotFather (см. ниже).

Запуск без Docker (PostgreSQL нужен отдельно):

```bash
uv sync
uv run alembic upgrade head
uv run uvicorn support_bot.main:create_app --factory --reload
```

## Переменные окружения

Полный список с комментариями — в [`.env.example`](.env.example). Значения берутся из
переменных окружения, при их отсутствии — из локального `.env`. Секретов в
репозитории нет. В production они приходят из пайплайна как env-переменные.

| Переменная | Назначение |
|------------|------------|
| `BOT_TOKEN` | токен бота (секрет) |
| `BOT_MODE` | `webhook` (production) или `polling` (разработка) |
| `WEBHOOK_SECRET` | секрет заголовка `X-Telegram-Bot-Api-Secret-Token`, обязателен для webhook |
| `WEBHOOK_URL` | публичный URL `…/telegram/webhook` для регистрации webhook |
| `WEBAPP_URL` | публичный URL Mini App `…/app/` |
| `DEV_CHAT_ID` | чат разработчиков для постов и напоминаний |
| `BOOTSTRAP_ADMIN_IDS` | Telegram ID первичных админов через запятую |
| `DATABASE_URL` | `postgresql+asyncpg://…` |
| `REMINDER_*_MINUTES` | интервалы напоминаний по приоритетам (по умолчанию 10 / 30 / 120 / 480) |
| `DRAFT_TTL_HOURS` | время жизни черновика формы (24) |
| `INIT_DATA_MAX_AGE_SECONDS` | максимальный возраст initData Mini App |
| `LOG_LEVEL`, `LOG_FORMAT` | уровень и формат логов (`json` / `console`) |
| `SENTRY_DSN` | DSN Sentry. Если пусто, Sentry выключен |
| `METRICS_PORT` | отдельный порт `/metrics` |

## Миграции

Миграции не запускаются при старте приложения: в Kubernetes их выполняет отдельный job
перед выкаткой. Автозапуск миграций есть только в dev-compose.

```bash
uv run alembic upgrade head                          # локально
docker run --rm --env-file prod.env support-bot:prod alembic upgrade head   # из prod-образа
uv run alembic revision --autogenerate -m "описание"  # новая миграция
```

`updated_at` во всех таблицах обновляет триггер БД `set_updated_at()`, поэтому
технические записи тоже его меняют. Инварианты статусов продублированы
CHECK-ограничениями.

## Тесты и качество

Тесты работают на реальном PostgreSQL. База `support_bot_test` пересоздаётся
миграциями в начале прогона.

```bash
# полностью в контейнерах
docker compose --profile test run --rm tests

# или локально: поднять тестовую БД (порт 55432) и запустить pytest
docker compose --profile test up -d test-db
uv run pytest
docker compose --profile test down

uv run ruff format --check . && uv run ruff check .
uv run mypy
```

URL тестовой БД переопределяется через `TEST_DATABASE_URL`.

## Сборка образов

Один multi-stage `Dockerfile`:

| Таргет | Назначение |
|--------|------------|
| `dev`  | все зависимости, editable-установка, `uvicorn --reload` |
| `test` | тестовые зависимости, по умолчанию запускает `pytest` |
| `prod` | минимальный образ, non-root (uid 10001), без dev-зависимостей, ровно один процесс uvicorn (без `--workers` и gunicorn); порты 8000 и 9000 |

```bash
docker build --target prod -t support-bot:prod .
```

Масштабирование — только числом реплик. Пробы: `GET /health/live` (liveness) и
`GET /health/ready` (readiness, проверяет соединение с БД) на основном порту.

## Регистрация в BotFather и webhook

1. `/newbot` создаёт бота и выдаёт токен (`BOT_TOKEN`).
2. Добавьте бота в чат разработчиков. Боту нужно право отправлять сообщения. Для
   кнопок под постами права администратора не требуются.
3. Mini App: `/mybots` → бот → *Bot Settings* → *Configure Mini App* (или *Menu Button*)
   → укажите `WEBAPP_URL`, например `https://support-bot.example.com/app/`. Если задан
   `WEBAPP_URL`, приложение при старте само ставит кнопку меню (то же делает
   `python -m support_bot.cli set-menu-button`). Web App-кнопки работают только в
   личных чатах, поэтому в чате разработчиков Mini App не показывается.
4. Webhook (production, после выкатки, HTTPS настраивает инфраструктура):
   ```bash
   # в окружении с BOT_TOKEN, WEBHOOK_URL, WEBHOOK_SECRET
   python -m support_bot.cli set-webhook
   python -m support_bot.cli webhook-info
   ```
   Telegram присылает секрет в заголовке `X-Telegram-Bot-Api-Secret-Token`. Запросы
   без верного секрета получают 403. Апдейт обрабатывается синхронно внутри запроса, и
   ответ всегда 200, даже при ошибке обработки: ошибка уходит в лог и Sentry.
5. Для локального polling webhook должен быть удалён. Приложение в режиме polling
   удаляет его само при старте (`python -m support_bot.cli delete-webhook`).

## Логи

Логи пишутся в stdout в формате JSON, по одному объекту на строку, и пригодны для
OpenSearch. В каждой записи есть `timestamp`, `level`, `logger`, `message` и
контекстные теги из contextvars: `request_id`, `update_id`, `telegram_user_id`,
`ticket_id`, `component`. В том же формате пишутся логи uvicorn, PTB и SQLAlchemy.
Для разработки есть `LOG_FORMAT=console`.

## Мониторинг

- **Sentry** включается, если задан `SENTRY_DSN`. Используются интеграции FastAPI,
  Starlette и SQLAlchemy. Ошибки хендлеров PTB, воркеров и отправки напоминаний уходят
  с тегами `component`, `update_id`, `ticket_id`, `telegram_user_id`.
- **Prometheus.** `/metrics` отдаётся на отдельном порту `METRICS_PORT`, а не на
  основном, чтобы не попадать наружу через ingress. В pod работает один процесс,
  поэтому используется стандартный registry без multiprocess-режима.

| Метрика | Тип | Labels |
|---------|-----|--------|
| `support_bot_http_requests_total` | counter | `method`, `route` (шаблон пути), `status` |
| `support_bot_http_request_duration_seconds` | histogram | `method`, `route` |
| `support_bot_tickets_created_total` | counter | `priority` |
| `support_bot_ticket_transitions_total` | counter | `action`, `from_status`, `to_status` |
| `support_bot_ticket_time_to_take_seconds` | histogram | `priority` |
| `support_bot_telegram_updates_total` | counter | `result`: processed / duplicate / failed / timeout |
| `support_bot_reminders_total` | counter | `result`: sent / failed |
| `support_bot_post_syncs_total` | counter | `result`: sent / edited / skipped / not_found / failed |
| `support_bot_errors_total` | counter | `component` |
| `support_bot_open_tickets` | gauge | `status` (new / in_progress / waiting_info) |

### Правила агрегации

- **Счётчики и гистограммы** считаются в каждом процессе отдельно. Значения по подам
  суммируются через `sum()`:
  ```promql
  sum by (priority) (rate(support_bot_tickets_created_total[5m]))
  histogram_quantile(0.9, sum by (le) (rate(support_bot_ticket_time_to_take_seconds_bucket[1h])))
  ```
- **`support_bot_open_tickets`** вычисляется запросом к БД при каждом скрейпе (custom
  collector), поэтому **каждая реплика отдаёт одно и то же значение**. Агрегировать его
  нужно через **`max()`, а не `sum()`**, иначе значение умножится на число реплик:
  ```promql
  max by (status) (support_bot_open_tickets)
  ```

Время до взятия отсчитывается от последнего входа в `new` (создание, отказ или
переоткрытие) по журналу `ticket_events`.

## Известные ограничения

- Если пост удалили вручную уже после успешной синхронизации, фоновая задача этого не
  заметит. Пост восстановится при следующем изменении обращения или вручную, кнопкой
  admin «Восстановить пост» в карточке Mini App.
- Если результат отправки нового поста неизвестен (например, таймаут), возможен редкий
  дубль поста. В БД сохраняется последний успешно отправленный.
- Напоминание, подготовленное до взятия обращения, может прийти уже после взятия.
- Каждый апдейт обрабатывается не больше одного раза. После сбоя пользователь
  повторяет действие (форма и кнопки защищены версиями), а обращение без поста
  опубликует фоновая синхронизация.
