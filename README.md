# Бот учёта обращений

Telegram-бот для первой линии поддержки и разработчиков: создание обращения в личном чате, общий пост в чате разработчиков, статусы, исполнитель, напоминания и Mini App. Общение по сути обращения происходит вне бота; автору не отправляются личные уведомления.

Стек: Python **3.14**, FastAPI, python-telegram-bot, PostgreSQL, SQLAlchemy/Alembic. Mini App — статические HTML/CSS/JS без сборки. Версии зависимостей закреплены в `uv.lock`; Python 3.13 и ниже не поддерживается.

## Локальный запуск

Нужны Docker Compose и uv. Создайте локальный конфиг:

```sh
cp .env.example .env
uv sync --frozen --extra dev --python 3.14
```

В `.env` задайте свой `BOT_TOKEN`, `DEV_CHAT_ID` и `BOOTSTRAP_ADMIN_IDS`. Добавьте бота в чат разработчиков с правами отправлять и редактировать собственные сообщения. Для локального запуска оставьте `BOT_MODE=polling`; одновременно запускайте одну polling-реплику для одного токена.

Приложение на хосте, PostgreSQL в Docker:

```sh
docker compose up -d postgres
uv run --frozen --extra dev alembic upgrade head
uv run --frozen --extra dev uvicorn support_bot.main:create_app --factory --host 0.0.0.0 --port 8000 --reload
```

`.env.example` содержит адрес БД на `localhost:5432`, соответствующий локальному Compose. Если порт занят, измените публикацию порта PostgreSQL и `DATABASE_URL` согласованно.

Полностью в Docker:

```sh
docker compose up --build
```

Dev Compose принудительно включает polling, использует адрес БД внутри сети Docker, запускает миграции и включает hot reload для `src/`. HTTP доступен на `http://localhost:8000`, метрики — на `http://localhost:9000/metrics`. Остановка: `docker compose down`; данные PostgreSQL сохраняются в volume.

Проверки доступности:

```sh
curl http://localhost:8000/health/live
curl http://localhost:8000/health/ready
curl http://localhost:9000/metrics
```

`live` проверяет работу HTTP-приложения; `ready` выполняет запрос к БД и возвращает 503 при недоступности PostgreSQL.

## Доступ и работа с ботом

Участники сначала пишут боту `/start` в личке. Telegram ID из `BOOTSTRAP_ADMIN_IDS` получают роль admin при первом `/start`; остальные обращаются к администратору. Список bootstrap не переопределяет роли после первого `/start`. Администратор назначает роли в Mini App:

- `support` создаёт обращения и просматривает свои и все обращения;
- `developer` берёт обращения кнопкой «Взять», затем меняет их статус;
- `admin` имеет обе роли по правам, управляет пользователями, переоткрывает и переназначает обращения, восстанавливает посты.

Команды: `/help`, `/new`, `/cancel`. Форма `/new`: описание → приоритет → ID заявки → необязательный ID арендатора → подтверждение. На текстовые вопросы отвечайте через Reply на актуальный вопрос. «Пропустить» для арендатора находится в отдельном сообщении. `/new` позволяет продолжить незавершённую форму или начать заново.

Состояния: `new` → `in_progress` ↔ `waiting_info` → `resolved` / `rejected` / `duplicate`. Исполнитель или admin может отказаться от активного обращения, вернув его в `new`. Закрытое обращение переоткрывает только admin. Чтобы снять с разработчика право быть исполнителем, сначала переназначьте его активные обращения.

## Mini App, BotFather и webhook

В BotFather создайте бота и получите токен. Настройте Mini App и кнопку меню бота с публичным HTTPS URL приложения, например `https://support.example.org/`. Укажите тот же адрес в `MINI_APP_URL`: после `/start` бот задаёт кнопку «Обращения» в личном чате. Можно также зарегистрировать команды `/start`, `/help`, `/new`, `/cancel` через BotFather. Для локальной Mini App нужен HTTPS-туннель к порту 8000 либо отдельный доступный HTTPS-стенд.

Mini App открывайте из Telegram: обычный браузер не получает подписанный `initData`. API использует заголовок `Authorization: tma <исходная строка Telegram.WebApp.initData>`. Сервер проверяет HMAC, срок действия и текущую роль в БД. `initDataUnsafe` не используется как доказательство личности.

Для production задайте через окружение:

- `BOT_MODE=webhook`;
- `WEBHOOK_URL=https://support.example.org/telegram/webhook`;
- `WEBHOOK_SECRET` — собственный секрет из 1–256 латинских букв, цифр, `_` и `-`;
- остальные обязательные параметры, включая `BOT_TOKEN`, `DATABASE_URL`, `DEV_CHAT_ID`.

После настройки внешнего HTTPS выполните регистрацию **один раз отдельной командой** в окружении с этими переменными:

```sh
python -m support_bot.manage register-webhook
```

В локальном uv-окружении эквивалент: `uv run --frozen python -m support_bot.manage register-webhook`. Telegram передаёт секрет в `X-Telegram-Bot-Api-Secret-Token`; прокси должен сохранять этот заголовок. Приложение не регистрирует webhook при старте. Polling при запуске снимает существующий webhook без удаления ожидающих обновлений — не запускайте его параллельно production.

Основные API: `GET /api/me`, `GET /api/tickets`, `GET /api/tickets/{id}`; admin: `POST /api/tickets/{id}/reopen`, `/reassign`, `/restore-post`, `GET /api/users`, `PATCH /api/users/{id}/role`. Списки поддерживают `page` и `page_size`; обращения — `scope`, `status`, `priority`. Бизнес-изменения передают `expected_version` и `expected_status`; устаревшее состояние возвращает 409.

## Конфигурация

Переменные окружения имеют приоритет над локальным `.env`. Полный шаблон с комментариями — [.env.example](.env.example). Ниже указаны значения по умолчанию класса настроек; пример может задавать другие демонстрационные значения. Секреты production передаются внешним пайплайном через окружение.

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `BOT_TOKEN` | обязательно | Токен из BotFather |
| `DATABASE_URL` | локальная БД `support`, см. `.env.example` | URL с префиксом `postgresql+asyncpg://` |
| `BOT_MODE` | `polling` | `polling` или `webhook` |
| `DEV_CHAT_ID` | `-1` | ID чата разработчиков; задайте настоящий ID |
| `BOOTSTRAP_ADMIN_IDS` | `[]` | Telegram ID через запятую или JSON-массив |
| `MINI_APP_URL` | пусто | Публичный HTTPS URL; пустое значение отключает установку кнопки меню |
| `WEBHOOK_URL` | пусто | Полный HTTPS URL для команды регистрации |
| `WEBHOOK_SECRET` | пусто | Обязателен в режиме webhook |
| `UPDATE_TIMEOUT_SECONDS` | `30` | Таймаут попытки обработки обновления |
| `INIT_DATA_MAX_AGE_SECONDS` | `3600` | Допустимый возраст Mini App initData |
| `AUTH_CLOCK_SKEW_SECONDS` | `30` | Допустимое опережение часов initData |
| `DRAFT_TTL_HOURS` | `24` | Срок неактивности черновика до очистки |
| `REMINDER_CRITICAL_SECONDS` | `600` | Интервал critical |
| `REMINDER_HIGH_SECONDS` | `1800` | Интервал high |
| `REMINDER_MEDIUM_SECONDS` | `7200` | Интервал medium |
| `REMINDER_LOW_SECONDS` | `28800` | Интервал low |
| `REMINDER_POLL_SECONDS` | `15` | Период проверки напоминаний |
| `POST_SYNC_POLL_SECONDS` | `10` | Период синхронизации постов |
| `CLEANUP_POLL_SECONDS` | `600` | Период очистки черновиков и dedup-записей |
| `WORKER_BATCH_SIZE` | `100` | Максимальный размер пакета, 1–1000 |
| `POST_LOCK_TIMEOUT_SECONDS` | `10` | Таймаут получения advisory lock обращения |
| `TELEGRAM_RETRY_ATTEMPTS` | `3` | Число попыток публикации поста, 1–10 |
| `TELEGRAM_RETRY_BASE_SECONDS` | `0.5` | Начальный backoff; учитывается Telegram retry_after |
| `TELEGRAM_TIMEOUT_SECONDS` | `10` | Сетевые таймауты Telegram |
| `DB_POOL_SIZE` | `10` | Размер пула на процесс, минимум 2 |
| `DB_MAX_OVERFLOW` | `10` | Дополнительные соединения пула |
| `HTTP_HOST` | `0.0.0.0` | Адрес production HTTP listener |
| `HTTP_PORT` | `8000` | Порт production HTTP listener |
| `METRICS_HOST` | `0.0.0.0` | Адрес отдельного listener метрик |
| `METRICS_PORT` | `9000` | Порт метрик, отличается от HTTP_PORT |
| `LOG_LEVEL` | `INFO` | DEBUG, INFO, WARNING, ERROR, CRITICAL |
| `LOG_FORMAT` | `json` | `json` либо `console` |
| `SENTRY_DSN` | пусто | Пустое значение отключает Sentry |
| `SENTRY_ENVIRONMENT` | `development` | Имя окружения Sentry |

Все интервалы, кроме явно обозначенных часов, заданы в секундах. Dev Compose фиксирует HTTP-порт 8000; для другого порта согласованно измените команду и публикацию портов.

## Проверки и образы

Интеграционные тесты работают с настоящим PostgreSQL и очищают таблицы между тестами. Используйте отдельную БД: её имя в `TEST_DATABASE_URL` должно содержать `test`. Без этой переменной интеграционные тесты пропускаются. Тестовый Compose создаёт временную БД и не использует dev-volume:

```sh
docker compose -f docker-compose.test.yml up --build --abort-on-container-exit --exit-code-from tests
docker compose -f docker-compose.test.yml down
```

Для PostgreSQL, доступного с хоста, создайте отдельную тестовую БД и задайте её URL:

```sh
TEST_DATABASE_URL='postgresql+asyncpg://<user>:<password>@localhost:5432/support_test' uv run --frozen --extra dev pytest -q
uv run --frozen --extra dev ruff check .
uv run --frozen --extra dev ruff format --check .
uv run --frozen --extra dev mypy src/support_bot
```

Тестовый fixture сам применяет миграции. Для параллельных независимых прогонов используйте разные тестовые БД. Команда `ruff format .` применяет форматирование. При необходимости `PTB_TIMEDELTA=true` включает поддерживаемый PTB формат `retry_after` и убирает предупреждение о будущем изменении его типа.

Dockerfile содержит targets `dev` (инструменты разработки и hot reload), `test` (pytest по умолчанию) и `prod` (без dev-зависимостей, пользователь UID 10001):

```sh
docker build --target prod -t support-bot:prod .
docker build --target test -t support-bot:test .
```

Production entry point `python -m support_bot` запускает один uvicorn-процесс, без `--workers` и gunicorn. Масштабирование — числом реплик в режиме webhook. Перед запуском новой версии выполните `alembic upgrade head` отдельной командой/job с доступом к БД. Production автоматически не запускает миграции и не регистрирует webhook. HTTPS, внешнее хранение секретов, CI/CD и Kubernetes-манифесты настраиваются инфраструктурой.

## Доставка, восстановление и наблюдаемость

PostgreSQL хранит обращения, журнал бизнес-событий, черновики и сроки напоминаний. Форма работает между репликами: текст принимается только как ответ на актуальный ForceReply-вопрос, кнопки содержат ID и версию черновика. При устаревшем ответе бот показывает текущий вопрос; `/new` → «Продолжить» восстанавливает форму после сбоя.

`update_id` фиксируется отдельной короткой транзакцией до обработки. Пока dedup-запись хранится, обновление получает не более одной попытки обработки; ошибка бизнес-обработки не отменяет claim. Записи хранятся 48 часов. После сбоя пользователь повторяет действие; версии защищают от устаревших кнопок.

Публикации одного обращения сериализуются session-level advisory lock на выделенном соединении. Сетевой запрос выполняется без открытой транзакции; успешная отправка сохраняет именно отрендеренную версию. Фоновая задача публикует обращения без поста и исправляет отставшие посты. При неопределённом результате отправки, например таймауте, допускается редкий дубль поста. Удаление уже синхронизированного поста вручную обнаружится при следующем изменении обращения; admin может сразу нажать «Восстановить пост» в Mini App.

Напоминания заранее получают новый срок через `FOR UPDATE SKIP LOCKED`, после коммита выполняется одна попытка отправки в чат разработчиков. Ошибка не ретраится; следующий срок считается от текущего времени, без догоняющей серии. Уже подготовленное напоминание может прийти после взятия обращения. Технические записи постов и напоминаний не меняют бизнес-версию и не добавляют события.

JSON-логи идут в stdout с контекстом запроса/обновления/пользователя/обращения. При заданном `SENTRY_DSN` ошибки отправляются в Sentry. Метрики слушают **отдельный внутренний порт 9000**; не публикуйте его через внешний ingress. Основной HTTP-порт не обслуживает `/metrics`.

Счётчики и гистограммы локальны для процесса: агрегируйте по репликам через `sum()`. `support_open_tickets{status}` каждый раз читается из общей БД и одинаков у реплик: агрегируйте через **`max()`**, иначе число обращений умножится на число реплик. Примеры PromQL для одного окружения:

```promql
sum by (priority) (rate(support_tickets_created_total[5m]))
sum by (result) (rate(support_updates_total[5m]))
sum(rate(support_reminders_sent_total[5m]))
sum by (component) (rate(support_errors_total[5m]))
histogram_quantile(0.95, sum by (le) (rate(support_ticket_time_to_take_seconds_bucket[1h])))
histogram_quantile(0.95, sum by (le, route) (rate(support_http_request_seconds_bucket[5m])))
max by (status) (support_open_tickets)
```

Добавляйте фильтры своего job/окружения, чтобы не объединять разные БД. HTTP-метрики используют шаблон маршрута, например `/api/tickets/{ticket_id}`, без ID в label.

Проверки используют подменённый Telegram transport и настоящий PostgreSQL; сборка тестового Docker-образа проверена. Работа с живым Telegram без настоящих учётных данных не проверялась. Развёртывание и регистрация webhook не выполнялись.
