# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Telegram bot + Mini App for tracking first-line support requests to the on-duty developer. `SPEC.md` (Russian) is the authoritative requirements document; consult it before changing behavior. User-facing text is Russian.

## Commands

Python 3.14, managed with `uv` (`pyproject.toml` + `uv.lock`). On this machine `uv` on PATH is a pyenv shim that may fail; if so use `~/.pyenv/versions/3.13.8/bin/uv`. Do not add a `.python-version` file (pyenv has no 3.14 and it breaks the shim).

```bash
uv sync                                   # install all groups (dev includes test)
docker compose --profile test up -d test-db   # test PostgreSQL on localhost:55432
uv run pytest                             # full suite (needs test-db)
uv run pytest tests/test_tickets.py::test_concurrent_take_only_one_wins   # single test
uv run ruff format . && uv run ruff check .
uv run mypy                               # strict, covers src/support_bot only
docker compose --profile test run --rm tests  # tests fully in containers
docker compose up --build                 # dev app (BOT_MODE=polling) + db, auto-migrates
uv run alembic upgrade head               # migrations are NOT run on app start in prod
uv run alembic check                      # verify models match migrations
docker build --target prod -t support-bot:prod .
```

Tests run against real PostgreSQL (`TEST_DATABASE_URL`, default `postgresql+asyncpg://postgres:postgres@localhost:55432/support_bot_test`). `tests/conftest.py` drops/recreates the DB and applies Alembic migrations once per session, then TRUNCATEs all tables before each test. pytest-asyncio runs in auto mode with a session-scoped loop. Telegram is replaced by `FakePublisher` (conftest) for services and `FakeBot` (`tests/test_bot_handlers.py`) for handlers — no network.

Entry point: `uvicorn support_bot.main:create_app --factory`. Ops CLI: `python -m support_bot.cli set-webhook|delete-webhook|webhook-info|set-menu-button`.

## Architecture

Layering is strict: `domain/` (pure: enums, state machine, errors, validation) → `services/` (business logic, owns transactions, no Telegram/HTTP imports) → thin adapters `telegram/handlers.py` and `api/routes.py`. External effects go through the `PostPublisher` protocol in `services/ports.py` (implemented by `telegram/publisher.py`). Wiring is in `container.py`; `main.py` lifespan builds the PTB `Application` (no Updater, no JobQueue), container, workers, and the metrics HTTP server.

The app runs as multiple k8s replicas, so **all coordination state lives in PostgreSQL** — never use in-process state, PTB persistence, ConversationHandler, or JobQueue. Key invariants that span files:

- **Ticket changes** (`services/tickets.py`): every business change is one conditional `UPDATE ... WHERE id AND version = expected AND status = expected` that increments `version`, plus a `ticket_events` row, in one transaction; permissions are checked from the DB role in that same transaction. Status transitions and their side effects (assignee, `taken_at`, `closed_at`, `next_reminder_at`) are defined in `domain/state_machine.py` (`TRANSITIONS`, `plan_transition`). Technical writes (`message_id`, `post_synced_version`, reminder rescheduling, draft `prompt_message_id`) must NOT bump `version`. Taking/reassigning locks the assignee user row `FOR SHARE`; role changes lock it `FOR UPDATE` and refuse to drop executor rights while the user has `in_progress`/`waiting_info` tickets.
- **Post sync** (`services/post_sync.py` + `db/advisory_lock.py`): the dev-chat post is a projection of the DB. Sync takes a session-level `pg_advisory_lock` on a dedicated AUTOCOMMIT connection, re-reads the ticket, renders (`rendering.py`, ≤4096 chars), sends/edits, then stores the *rendered* version in `post_synced_version`. The lock is released in `finally`; if unlock fails the connection is invalidated, never returned to the pool. Call `sync_safely` after committing a change; the `post_sync` worker picks up tickets with `message_id IS NULL OR post_synced_version < version`.
- **Draft form** (`services/drafts.py`, `telegram/form_ui.py`): one draft per user in `ticket_drafts`; successful step changes happen under `SELECT ... FOR UPDATE` and bump draft `version` once. A text answer is accepted only if `reply_to_message.message_id == prompt_message_id`; otherwise the current question is re-asked. After commit the next question is sent and its id saved via conditional `save_prompt(draft_id, version, step, msg_id)`. Submit creates the ticket and deletes the draft in one transaction.
- **Reminders** (`services/reminders.py`): `next_reminder_at` is set/cleared in the same transaction as entering/leaving `new`; the worker claims due rows with `FOR UPDATE SKIP LOCKED`, reschedules from `now()`, commits, then sends once (no retry).
- **Update dedup** (`telegram/pipeline.py`): `update_id` is committed to `processed_updates` in its own transaction before processing; webhook and polling share this pipeline. Processing runs in a separate task (isolated contextvars) under a timeout; webhook always returns 200.
- `callbacks.py` defines the compact `callback_data` formats (`t:<action>:<id>:<version>`, `d:<action>:<uuid-b64>:<version>[:<arg>]`, ≤64 bytes); buttons always carry the expected version.
- Mini App (`webapp/`, vanilla JS, served at `/app/`) authenticates via `Authorization: tma <initData>` validated in `api/auth.py`; domain errors map to HTTP codes in `api/errors.py` (stale version → 409).

DB conventions: `updated_at` is maintained by the `set_updated_at()` trigger (not ORM onupdate); status invariants are duplicated as CHECK constraints; Alembic migrations are hand-written (ENUM types are created explicitly with `create_type=False`), so run `alembic check` after model changes. Ticket number is the identity PK `tickets.id`. DB time (`func.now()`) is used for all timestamps and schedules.

Observability: structlog JSON to stdout with contextvar tags (`request_id`, `update_id`, `telegram_user_id`, `ticket_id`, `component`); Sentry only if `SENTRY_DSN` set (`monitoring/sentry.capture_exception`); Prometheus on separate `METRICS_PORT`. The `support_bot_open_tickets` gauge is computed per scrape from the DB and must be aggregated with `max()`, not `sum()`.
