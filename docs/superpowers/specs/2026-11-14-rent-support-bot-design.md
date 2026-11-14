# Дизайн: Telegram-бот учёта обращений к дежурному разработчику

Дата: 2026-11-14
Источник требований: `SPEC.md`

## Цель

Сервис учёта обращений первой линии поддержки к дежурному разработчику:
бот для создания обращений и смены статусов, пост-карточка в чате
разработчиков, Mini App для просмотра и админских действий, фоновые
напоминания и синхронизация постов. Бот не управляет графиком дежурств.

## Стек и совместимость

- Python 3.14.2, проверены: FastAPI 0.162, PTB 22.8, SQLAlchemy 2.1.6,
  asyncpg 0.32, Alembic 1.20, pydantic 2.14 / pydantic-settings 2.16,
  structlog 26.1, sentry-sdk 2.73, prometheus-client 0.26.
- PostgreSQL, SQLAlchemy async + asyncpg, Alembic.
- Mini App: vanilla JS + Telegram WebApp SDK, без сборки.
- uv + `pyproject.toml` + `uv.lock`; ruff, mypy, pytest + pytest-asyncio.

## Архитектурные решения

1. Номер обращения — identity PK `tickets.id`, отображается как `#123`.
2. Бизнес-логика — в `services/`, не зависит от Telegram/FastAPI.
   Telegram-публикатор и HTTP-слой подключаются через протоколы/тонкие адаптеры.
3. Все бизнес-изменения тикета — один условный `UPDATE` с проверкой
   `id`, `version`, `status` и инкрементом `version`; права и блокировки
   строк исполнителя проверяются в той же транзакции.
4. `updated_at` — только SQLAlchemy `onupdate=func.now()`, без триггеров;
   все записи идут через Core/ORM.
5. Черновик формы — в `ticket_drafts`, версия + условные обновления;
   `ForceReply` и проверка `reply_to_message.message_id`.
6. Синхронизация поста — выделенное физическое asyncpg-соединение с
   `pg_try_advisory_lock(ticket_id)`, рендер актуального состояния,
   отправка/редактирование, сохранение отрендеренной version.
7. Напоминания и фоновый пост-синк — in-process задачи в каждой реплике,
   `SKIP LOCKED` / try-lock; schedule хранится в БД.
8. Дедупликация апдейтов — отдельная короткая транзакция
   `INSERT ... ON CONFLICT DO NOTHING` до бизнес-обработки.
9. Метрики — стандартный registry; `/metrics` на отдельном порту,
   gauge «открытые по статусу» считается запросом к БД при скрейпе.
   Счётчики/гистограммы агрегируются `sum()`, gauge — `max()`.
10. Логи — structlog JSON/console, контекст через contextvars.

## Схема БД

- `users`: id, telegram_id (unique), username, first_name, last_name,
  role (nullable enum), created_at, updated_at.
- `tickets`: id (identity/номер), author_id, assignee_id, description,
  priority, request_id, tenant_id, status, version (default 1),
  created_at, updated_at, taken_at, closed_at, next_reminder_at,
  message_id, post_synced_version (default 0) + CHECK-инварианты.
- `ticket_events`: id, ticket_id, event_type, actor_id, old/new status,
  old/new assignee, version, created_at.
- `ticket_drafts`: id uuid, user_id (unique, один активный), step,
  поля формы, version, prompt_message_id, created_at, updated_at.
- `processed_updates`: update_id PK, created_at.

Enum-типы: `user_role`, `ticket_status`, `ticket_priority`,
`ticket_event_type`, `draft_step`.

Инварианты CHECK: описание 1..3000, request_id 1..64, tenant_id 1..64
или NULL, version >= 1, `new` без исполнителя/taken_at, остальные с ними,
`closed_at` iff финальный статус, `next_reminder_at` iff `new`.

## State machine

Действия: take, wait_info, to_work, resolve, reject, duplicate, decline,
reopen, reassign. Таблица переходов и проверки прав — явные, с тестами.
- take: new → in_progress, developer/admin; lock исполнителя FOR SHARE.
- wait_info / to_work / resolve / reject / duplicate: исполнитель или admin.
- decline: in_progress/waiting_info → new, исполнитель или admin.
- reopen: финальный → new, только admin.
- reassign: in_progress/waiting_info, только admin, target developer/admin.

## Фоновые процессы

- post sync worker: `message_id IS NULL OR post_synced_version < version`.
- reminder worker: `status='new' AND next_reminder_at <= now()`,
  `FOR UPDATE SKIP LOCKED`, сдвиг срока после коммита, отправка без ретраев.
- cleanup worker: черновики старше TTL, processed_updates старше 48 ч.

## API Mini App

`GET /api/me`, `GET /api/tickets`, `GET /api/tickets/{id}`,
admin: `POST /api/tickets/{id}/reopen`, `/reassign`, `/restore-post`,
`GET /api/users`, `GET /api/users/assignable`,
`PATCH /api/users/{id}/role`.
Авторизация — HMAC-SHA256 initData + проверка auth_date.

## Этапы

1. Каркас, конфиг, логирование, БД-сессия.
2. Модели, Alembic-миграция, тестовый каркас на PostgreSQL.
3. State machine, сервисы тикетов/ролей/журнала.
4. Рендер и синхронизация поста.
5. Бот: команды, черновики, кнопки, webhook/polling.
6. Напоминания и cleanup.
7. API Mini App и статика.
8. Метрики и Sentry.
9. Docker/compose.
10. README и финальная проверка.