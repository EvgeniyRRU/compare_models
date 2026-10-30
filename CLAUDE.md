# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repo state

`SPEC.md` (Russian) is the source of truth; the stages in it are implemented. Code is in `src/app` (src layout, package `app`), tests in `tests/`, migrations in `alembic/`. UI language is Russian. Items marked **[допущение]** in the spec are assumptions that can be challenged; ask rather than guess on ambiguities.

## Stack

Python 3.14 (`.venv` is managed by uv; `uv.lock` is committed), FastAPI, python-telegram-bot 22 (Application without Updater), PostgreSQL + SQLAlchemy 2 async (asyncpg) + Alembic, pydantic-settings, structlog, sentry-sdk, prometheus_client, vanilla-JS Mini App in `src/app/web/static`.

## Commands

Postgres must be up for tests: `docker compose up -d db` (host port 5433).

- All tests: `.venv/bin/pytest`; one test: `.venv/bin/pytest tests/test_tickets.py::test_take_race_only_one_wins`. The `support_bot_test` DB is created automatically and its schema is built by running the Alembic migrations (so migrations are exercised by every run). Override with `TEST_DATABASE_URL`.
- Lint/format/types: `.venv/bin/ruff check . && .venv/bin/ruff format --check .`, `.venv/bin/mypy` (strict, `src/app` only).
- Migrations: `DATABASE_URL=... alembic upgrade head` / `alembic revision --autogenerate -m msg --rev-id 000N`. Never run on app startup (compose dev does it explicitly).
- Run locally: `docker compose up --build` (polling mode); images: `docker build --target dev|test|prod .`

## Architecture constraints that span many files

The app runs as **multiple replicas in Kubernetes, one uvicorn process per pod**. Nothing may rely on in-process state:

- **Layering**: `domain/state_machine.py` is pure (no DB); `services/` hold business logic and never commit (callers wrap them in `async with sm() as s, s.begin()`); `bot/handlers.py` and `api/routes.py` stay thin. Post sync talks to Telegram only through the `Publisher` protocol (`tests/fakes.py` has a fake).
- **Roles**: one nullable `role` per user (`support`/`developer`/`admin`), read from DB on every update/API call (never cached). Role downgrade of a developer/admin with `in_progress`/`waiting_info` tickets is forbidden; role change locks the user row `FOR UPDATE`, take/reassign lock the assignee row `FOR SHARE`.
- **Ticket state machine** is explicit and tested. Every business mutation is a single conditional `UPDATE ... WHERE id AND version = :expected AND status = :expected`, bumps `version` exactly once, and writes one `ticket_events` row in the same transaction. Technical writes (`message_id`, `post_synced_version`, `next_reminder_at` rescheduling after a reminder fires, "restore post") must NOT bump `version` and write no event. Invariants are enforced in both service layer and DB CHECK constraints (`new` ⇒ no assignee/`taken_at`; `in_progress`/`waiting_info` ⇒ both set; `closed_at` set iff final status). Ticket number is a DB identity column. Callback data format `t:<action>:<id>:<version>` (≤64 bytes).
- **`updated_at`**: SQLAlchemy `onupdate=func.now()` on every table (applies to Core `update()` too; don't set it by hand).
- **Ticket creation form**: do NOT use `ConversationHandler` or PTB persistence. State is in `ticket_drafts` (UUID id, one active per user, step, fields, `version`, `prompt_message_id`). Text answers are accepted only as replies to the stored `prompt_message_id` (ForceReply is UX only). Callbacks embed draft id+version; stale ones are rejected without changing data/step/version. `prompt_message_id` is saved via conditional `UPDATE ... WHERE id AND version AND step`, never holding a transaction open during Telegram calls. "Отправить" creates the ticket and deletes the draft in one transaction.
- **Post sync** (Telegram dev-chat post is a view of the DB): one procedure used everywhere — session-level `pg_advisory_lock` per ticket on a dedicated connection with timeout, always unlocked in `finally` (close the connection if unlock fails), re-read ticket under the lock, render, send/edit (republish if deleted), then save the *rendered* version as `post_synced_version`. A background task in each replica re-syncs tickets with null `message_id` or `post_synced_version < version` using `pg_try_advisory_lock`. Post text must fit 4096 chars (truncate description with «…»). Retry Telegram errors with backoff honoring `retry_after`; "message is not modified" is success.
- **Reminders**: schedule lives in DB (`next_reminder_at`), not PTB JobQueue. Worker: in one transaction `SELECT ... FOR UPDATE SKIP LOCKED` due `new` tickets, shift `next_reminder_at = now() + interval`, commit; only then send (at-most-once, no retries). Entering `new` sets it, leaving `new` clears it, as part of the same transition.
- **Webhook/polling**: `POST /telegram/webhook` checks `X-Telegram-Bot-Api-Secret-Token`, processes synchronously with a timeout, always returns 200. Dedup first: `INSERT ... ON CONFLICT DO NOTHING` into `processed_updates` committed in its own short transaction *before* business handling (handler failure must not roll it back); purge rows >48h. Same mechanism in `BOT_MODE=polling` (local dev); production is `webhook`. PTB lifecycle is bound to FastAPI lifespan.
- **Mini App auth**: validate `initData` HMAC-SHA256 (bot-token-derived key) + `auth_date` freshness; permissions from DB role. Admin-only actions: reopen, reassign, restore post, role management (role-change error returns the list of blocking tickets).
- **Observability**: `/metrics` on a separate `METRICS_PORT` (not the main port); standard prometheus registry (no multiprocess); HTTP metrics label by route template; the "open tickets by status" gauge is a DB-querying custom collector, aggregated with `max()` across pods (counters/histograms with `sum()`) — document in README. structlog contextvars tags: request_id, update_id, telegram_user_id, ticket_id, component. Sentry DSN optional.
- **Docker**: single multi-stage Dockerfile with `dev`, `test`, `prod` targets (prod: non-root, exactly one uvicorn process, no gunicorn/`--workers`). Compose: app (dev, polling) + Postgres, plus a separate profile/file for tests. No CI/CD or k8s manifests in scope. Config via env with `.env` fallback; ship `.env.example`, no secrets in repo.
