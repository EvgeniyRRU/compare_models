# План реализации: rent_support_bot

Источник: `SPEC.md`, дизайн: `docs/superpowers/specs/2026-11-14-rent-support-bot-design.md`.

Каждый этап заканчивается рабочим кодом и прогоном тестов.

## 1. Каркас и конфиг
- `pyproject.toml`, `uv.lock`, `.gitignore`, `.env.example`.
- `config.py`, `logging.py`, `db/base.py`, `db/session.py`.
- Проверка: `python -c "import support_bot"`, ruff.

## 2. Модели и миграции
- Модели users/tickets/ticket_events/ticket_drafts/processed_updates.
- Начальная Alembic-миграция, async `env.py`.
- `tests/conftest.py`: реальный PostgreSQL, миграции, truncate, фабрики.
- Проверка: `alembic upgrade head`, тест схемы.

## 3. State machine и сервисы
- `domain/state_machine.py`, `domain/errors.py`, `domain/reminders.py`.
- `services/users.py`, `services/tickets.py`, `services/updates.py`.
- Тесты: переходы, права, version, конкурентное взятие, роли,
  запрет снятия роли при активных обращениях.

## 4. Синхронизация поста
- `services/post_renderer.py`, `services/post_sync.py`,
  адаптер Telegram, advisory lock.
- Тесты: 4096, повторный выбор, гонки, lock при ошибке/отмене,
  force_new, фоновая публикация.

## 5. Бот
- `bot/callbacks.py`, `keyboards.py`, `application.py`, `polling.py`,
  handlers: commands, draft_form, ticket_actions, errors.
- `services/drafts.py`.
- Тесты черновиков: конкуренция, чужие/устаревшие кнопки, привязка
  reply, запоздавшая отправка вопроса, повторный confirm.

## 6. Напоминания и cleanup
- `services/reminders.py`, `workers/*`.
- Тесты: расписание, отсутствие догоняющих серий, SKIP LOCKED,
  version не меняется.

## 7. API Mini App
- `api/auth.py`, `api/schemas.py`, routes, статика.
- Тесты initData, scopes/фильтры/пагинация, admin-действия,
  конфликт ролей.

## 8. Метрики и Sentry
- `observability/metrics.py`, middleware, metrics-сервер, Sentry.
- Тесты middleware и gauge.

## 9. Docker/compose
- multi-stage `dev`/`test`/`prod`, compose с PostgreSQL и test-профилем.

## 10. README и финал
- Запуск, env, миграции, образы, BotFather, агрегация метрик.
- `ruff check`, `ruff format --check`, `mypy`, `pytest`.