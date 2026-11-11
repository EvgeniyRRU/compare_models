# CLAUDE.md

Guidance for Claude Code when working in this repository. The full product and technical specification is in `SPEC.md` (in Russian) and is the source of truth; this file holds the rules that must hold across every change.

## Project

Telegram bot for tracking first-line support requests (обращения) to the on-duty developer. The bot records tickets, tracks status and assignee, and lets users view their own and all tickets. It does not manage the on-call schedule: whoever presses «Взять» takes the ticket. The conversation about the ticket happens outside the bot.

- UI texts and bot messages: Russian.
- Code, identifiers, comments, commit messages: English.
- Stack: Python 3.14, FastAPI, python-telegram-bot v21+ (async, no Updater), PostgreSQL, SQLAlchemy 2.x async (asyncpg), Alembic, pydantic-settings, structlog, prometheus_client, sentry-sdk. Mini App: vanilla JS + Telegram WebApp SDK served as static files by FastAPI, no build step.
- Status: all stages of `SPEC.md` § «Порядок работы» are implemented. `README.md` documents setup, env vars, migrations, images, BotFather steps and metric aggregation.

## Commands

- Database for tests and local dev: `docker compose up -d db` (PostgreSQL 17 on host port 5433). Tests create the `support_bot_test` database themselves; override with `TEST_DATABASE_URL`.
- Tests: `uv run pytest` (or `docker compose --profile test run --rm test`). Tests run on real PostgreSQL, never SQLite or mocks of the database. Telegram is replaced by `tests/fake_telegram.py` (a `BaseRequest` double), so PTB dispatch runs for real.
- Lint and format: `uv run ruff check .` and `uv run ruff format --check .`.
- Types: `uv run mypy` (strict, on `src/support_bot`).
- Migrations: `alembic upgrade head` as a separate command, never on application startup. New migration: `alembic revision --autogenerate -m "..."`, then review it by hand.
- Local app: `docker compose up --build app` (polling mode, migrations run first).

Run ruff, mypy and pytest before reporting a task as done, and report the actual output if something fails.

## Architecture rules

- Business logic lives in a service layer that does not import Telegram or HTTP code. Bot handlers and API endpoints stay thin: parse input, call the service, render output.
- Do NOT use `ConversationHandler` or PTB `BasePersistence`. Multi-step forms are stored in the `ticket_drafts` table, because several replicas handle updates of one user.
- Do NOT use the in-memory PTB `JobQueue`. Background work (reminders, post sync) runs on a schedule kept in PostgreSQL, using `SELECT ... FOR UPDATE SKIP LOCKED` and advisory locks.
- The Telegram webhook is processed synchronously inside the request (`await application.process_update(...)`). Errors are logged and sent to Sentry, but the response is still 200.
- `processed_updates` is written and committed in its own short transaction before business handling. A business error must not roll it back.

## Ticket invariants (must hold in code and in DB CHECK constraints)

- Every business change of an existing ticket is one conditional `UPDATE ... WHERE id = :id AND version = :expected_version AND status = :expected_status` that increments `version` by 1. Permissions are checked in the same transaction.
- Technical writes (saving `message_id`, `post_synced_version`, moving `next_reminder_at` when a reminder fires, «Восстановить пост») do NOT change `version` and are NOT written to `ticket_events`.
- Every business change writes a row to `ticket_events` in the same transaction.
- `new` has no assignee and no `taken_at`. `in_progress` and `waiting_info` always have an assignee and `taken_at`. `closed_at` is set if and only if the status is final (`resolved`, `rejected`, `duplicate`).
- Transition `new` → final statuses is forbidden. Reopen and reassign are admin-only.
- Role checks read the role from the database on every update and API request. Do not cache roles.
- Changing a developer/admin role to one without assignee rights is blocked while the user has tickets in `in_progress` or `waiting_info`. The user row is locked `FOR UPDATE`; taking and reassigning lock the assignee row `FOR SHARE`.
- `callback_data` is limited to 64 bytes. Use compact formats such as `t:<action>:<id>:<version>`.

## Ticket post synchronization

- The database is the source of truth; the post in the developer chat is only a rendering.
- Sync: take a session-level advisory lock (`pg_try_advisory_lock` / `pg_advisory_lock` with timeout) on a dedicated physical connection; read the current state from the DB under the lock; send or edit the post; save `post_synced_version` (the version that was rendered, not the current one) and `message_id` in one transaction; `pg_advisory_unlock` in `finally`. If unlock fails, close the connection instead of returning it to the pool.
- Never resend a previously prepared text. Render from the DB state under the lock.
- Rendered post must fit into 4096 characters; the description is truncated with «…».

## Reminders

- `next_reminder_at` is set in the same transaction on entering `new` (create, reject, reopen) and cleared on leaving `new`.
- After firing, the next time is `now() + interval`, not "catch up". No more than one send attempt per due time.

## Mini App and auth

- Validate `initData` with HMAC-SHA256 using the bot token, check `auth_date` freshness against config.
- Access is determined by the role in the database.

## Logging, metrics, config

- Structured JSON logs to stdout via structlog, with context tags in contextvars (`request_id`, `update_id`, `telegram_user_id`, `ticket_id`, `component`). Format and level come from config.
- Metrics: `/metrics` on `METRICS_PORT`, not on the main port. Use the default registry; the app runs one uvicorn process per pod.
- The "open tickets by status" gauge is aggregated with `max()` across replicas, counters with `sum()`. Document this in the README.
- Prod image runs exactly one uvicorn process (no `--workers`, no gunicorn).
- Configuration: pydantic-settings, env with fallback to `.env`. `.env.example` lists every variable with comments. No secrets in the repository.

## Testing expectations

- Required coverage areas are listed in `SPEC.md` § «Качество». Tests for state machine, permissions, version/optimistic checks, post sync, update deduplication, draft form, reminders, role-change guard, and Mini App API auth are not optional.
- Write the failing test first for bug fixes and new rules. Check that a test can fail: a test that passes before the fix proves nothing.

## Working agreements

- Follow the spec's ordering: propose structure, DB schema and plan before large implementation steps when the spec asks for it, and ask instead of guessing on ambiguous points. Items marked **[допущение]** in `SPEC.md` are assumptions and may be changed after confirmation.
- Do not lower the Python version silently. If a dependency does not support 3.14, report it first.
- Do not commit `.venv`, caches (`.mypy_cache`, `.pytest_cache`, `.ruff_cache`), `.env` or secrets.
