# Telegram-бот учёта обращений первой линии поддержки

Телеграм-бот, регистрирующий заявки пользователей и автоматически отправляющий их в дежурную группу разработчиков.

## Локальный запуск

1. Скопируйте `.env.example` в `.env` и заполните обязательные переменные:

   ```bash
   cp .env.example .env
   ```

   Обязательные: `BOT_TOKEN`, `DEV_CHAT_ID`, `DATABASE_URL`, `BOOTSTRAP_ADMIN_IDS`.

2. Запуск через Docker Compose (сборка dev-образа, Postgres + приложение с hot-reload на порту 8000):

   ```bash
   docker compose up --build
   ```

   По умолчанию используется polling-режим получения обновлений. Исходный код приложения (`app/`) примонтирован в контейнер — изменения подхватываются автоматически.

3. Альтернативный запуск без Docker:

   ```bash
   pip install -e ".[dev]"
   # PostgreSQL должен быть доступен на localhost:5433 (порт docker compose db)
   alembic upgrade head
   BOT_MODE=polling uvicorn app.main:app --reload
   ```

## Переменные окружения

См. [`.env.example`](.env.example) — все переменные описаны с комментариями.

Ключевые переменные:

| Переменная | Описание |
|---|---|
| `BOT_TOKEN` | Токен бота, полученный у @BotFather (обязательная) |
| `DEV_CHAT_ID` | ID чата дежурной группы (обязательная) |
| `DATABASE_URL` | URL подключения к PostgreSQL (asyncpg) |
| `BOOTSTRAP_ADMIN_IDS` | ID пользователей, получающих роль admin при первом `/start` (через запятую) |
| `BOT_MODE` | `polling` (по умолчанию) или `webhook` |
| `APP_PORT` | Порт FastAPI-приложения (по умолчанию `8000`) |
| `METRICS_PORT` | Порт Prometheus-метрик (по умолчанию `9090`) |

## Миграции

Применить миграции:

```bash
alembic upgrade head
```

При использовании Docker Compose:

```bash
docker compose exec app alembic upgrade head
```

Начальная миграция уже находится в `migrations/versions/`.

## Сборка образов

```bash
# Dev-образ (hot reload, dev-зависимости)
docker build --target dev -t rent-bot:dev .

# Test-образ (только тестовые зависимости, точка входа — pytest)
docker build --target test -t rent-bot:test .

# Prod-образ (минимальный, запускается от непривилегированного пользователя appuser)
docker build --target prod -t rent-bot:prod .
```

Prod-образ:
- Запускается от пользователя `appuser` (не root).
- Открывает порты `8000` (приложение) и `9090` (метрики Prometheus).

## Тесты

Запуск тестов в Docker с чистой базой PostgreSQL:

```bash
docker compose -f docker-compose.test.yml up --build --abort-on-container-exit
```

Эта команда собирает test-образ, поднимает свежий экземпляр Postgres, выполняет `pytest -v` и завершает все контейнеры по окончании тестов.

## Вебхук (Telegram)

Для работы в режиме webhook необходимо зарегистрировать URL у BotFather:

```
https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://your-domain/telegram/webhook&secret_token=<X-Telegram-Bot-Api-Secret-Token>
```

В production-окружении установите переменные:

- `BOT_MODE=webhook`
- `WEBHOOK_URL` — публичный HTTPS-адрес (например, `https://your-domain`)
- `WEBHOOK_SECRET` — секретный токен, совпадающий с `secret_token` в URL выше

## Mini App (Telegram)

Для регистрации Mini App используйте BotFather:

```
/setmenubutton → выбрать бота → задать текст кнопки и URL (например, https://your-domain/)
```

Статический фронтенд Mini App раздаётся с корневого пути `/` того же FastAPI-приложения.

## Метрики (Prometheus)

Метрики доступны на порту `9090` по пути `/metrics`.

Рекомендации по агрегации в multi-pod окружении:

- Счётчики — агрегируются через `sum()` по всем pod'ам.
- `open_tickets_by_status` — gauge, все реплики показывают одинаковое значение; агрегируйте через `max()`.

Пример scrape-конфигурации:

```yaml
scrape_interval: 15s
honor_labels: true
```