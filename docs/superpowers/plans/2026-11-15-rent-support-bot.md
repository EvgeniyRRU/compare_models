# Rent Support Bot (Telegram duty-developer ticket tracker) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Telegram bot + FastAPI + Mini App accounting first-line support requests to the on-duty developer: tracked tickets with role-based workflow, dev-chat post sync, reminders, metrics.

**Architecture:** Service layer (`app/services`) owns all business logic against PostgreSQL; PTB handlers and REST endpoints are thin adapters. Every ticket mutation is one conditional `UPDATE ... WHERE id AND version AND status` in a transaction that also writes a `ticket_events` row. Dev-chat posts are projections synced via advisory-locked `post_sync` procedure with a background sweeper. Draft creation form is DB-backed (multi-replica safe), not ConversationHandler.

**Tech Stack:** Python 3.14 (Docker-only toolchain), FastAPI, python-telegram-bot ~22.8 (async, no Updater; manual polling loop reuses the same `process_update` path as webhook), SQLAlchemy 2.x async + asyncpg, Alembic, PostgreSQL 17, pydantic-settings, structlog, sentry-sdk, prometheus-client, pytest/pytest-asyncio against real PG from compose, ruff, mypy, vanilla JS Mini App.

**Spec assumptions accepted:** priorities critical/high/medium/low; request_id only non-empty ≤64; «Отказаться» by assignee+admin; reminder intervals 10m/30m/2h/8h; structlog; vanilla JS WebApp; DB trigger for `updated_at`; ticket number = identity PK `id`.

---

## Conventions

- Run everything in Docker: `docker compose run --rm test <cmd>` (pytest/ruff/mypy/alembic). Never local venv.
- Conventional commits per phase (`chore:`, `feat:`, `test:`, `docs:`).
- All timestamps come from DB `now()` (UTC timestamptz); Python never supplies business timestamps — keeps tests clock-consistent.
- callback_data formats (<64 bytes): tickets `t:<action>:<id>:<version>`, drafts `d:<uuid32hex>:<action>:<version>` where action ∈ `pC,pH,pM,pL,sk,ok,cn,ct,rs`.
- Domain errors → HTTP / alerts mapping centralized in one FastAPI exception handler + one PTB callback wrapper.

## Task 0: Docker toolchain + repo scaffolding

**Files:** `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `.gitignore`, `.env.example`, `pyproject.toml`, `alembic.ini`, `docker/init-db.sh`, `tests/conftest.py`, `tests/test_smoke.py`

- [ ] pyproject with runtime deps (fastapi, uvicorn[standard], python-telegram-bot~=22.8, sqlalchemy[asyncio]>=2,<3, asyncpg, alembic, pydantic-settings, structlog, sentry-sdk, prometheus-client, httpx) and `[project.optional-dependencies] dev` (pytest, pytest-asyncio, ruff, mypy, types-PyYAML, time-machine). Ruff (line 100, rules E,F,I,UP,B,ASYNC,SIM,RUF), mypy (plugins pydantic), pytest asyncio_mode=auto.
- [ ] Multi-stage Dockerfile from `python:3.14-slim`: `deps` stage installs via tomllib-parsed pyproject (no package build needed); targets `dev` (runtime+dev deps, source bind-mounted, `uvicorn --reload`), `test` (runtime+dev deps, CMD `pytest`), `prod` (runtime deps only, non-root user, single `uvicorn` process, EXPOSE 8000/9090).
- [ ] compose: `postgres:17-alpine` (dual DB `rent_bot`/`rent_bot_test` via init script, 127.0.0.1:5433:5432, healthcheck, volume), `app` (dev target, BOT_MODE=polling, `alembic upgrade head && uvicorn --reload`), `test` service (profiles=[test], source mounted, DATABASE_URL→test DB).
- [ ] `.env.example` with every var + comments; `.gitignore` covers `.env`, venv, caches.
- [ ] conftest: session-scoped `alembic upgrade head` subprocess against test URL; function-scoped engine/session fixtures; `TRUNCATE ... RESTART IDENTITY CASCADE` between tests.
- [ ] Verify: `docker compose run --rm test pytest tests/test_smoke.py` passes (imports + PG connectivity smoke).

## Task 1: Skeleton, config, logging, health

**Files:** `app/config.py`, `app/logging_setup.py`, `app/main.py`, `app/sentry.py`, `tests/test_config.py`, `tests/test_health.py`

- [ ] `Settings(BaseSettings)`: BOT_TOKEN, BOT_MODE(=polling), WEBHOOK_SECRET_TOKEN, DEV_CHAT_ID, BOOTSTRAP_ADMIN_IDS (comma-separated ints), DATABASE_URL, PORT=8000, METRICS_PORT=9090, LOG_LEVEL/LOG_FORMAT(json|console), DRAFT_TTL_HOURS=24, AUTH_DATE_MAX_AGE_SECONDS=3600, PROCESS_UPDATE_TIMEOUT_SECONDS=30, POST_SYNC_INTERVAL_SECONDS=30, REMINDER_POLL_INTERVAL_SECONDS=30, TG_RETRY_ATTEMPTS=5, reminder intervals per priority (minutes), SENTRY_DSN optional. `.env` fallback.
- [ ] structlog setup: JSON to stdout, contextvars whitelist (request_id, update_id, telegram_user_id, ticket_id, component), stdlib logging bridged; `configure_logging(settings)`.
- [ ] FastAPI factory `create_app(settings)`: lifespan placeholder, `GET /health/live`, `GET /health/ready` (SELECT 1), static miniapp mount `/miniapp`.
- [ ] sentry init skipped when DSN empty.
- [ ] Tests: defaults parse; comma admin ids; health live OK without DB; ready 503 on broken DB, 200 with.

## Task 2: Models + initial migration

**Files:** `app/db/base.py`, `app/db/models.py`, `app/db/session.py`, `app/migrations/env.py`, `app/migrations/script.py.mako`, `app/migrations/versions/0001_initial.py`, `tests/test_models.py`

- [ ] PG native enums: `role(support,developer,admin)`, `priority(critical,high,medium,low)`, `ticket_status(new,in_progress,waiting_info,resolved,rejected,duplicate)`, `draft_step(description,priority,request_id,tenant_id,confirm)`, `event_type(created,taken,status_changed,abandoned,reassigned,reopened)`.
- [ ] Tables per approved schema. tickets CHECKs: `new⇒assignee IS NULL AND taken_at IS NULL`; `in_progress|waiting_info⇒assignee NOT NULL AND taken_at NOT NULL`; `closed_at NOT NULL ⇔ status IN final`. Indexes: `(status,next_reminder_at) WHERE status='new'`, `assignee_id`, `author_id`, `post_synced_version<version` partial index `(id) WHERE message_id IS NULL OR post_synced_version<version`.
- [ ] `updated_at` via DB trigger function `set_updated_at()` on all tables having it (migration ships trigger DDL).
- [ ] ticket_drafts.user_id UNIQUE; processed_updates.update_id UNIQUE.
- [ ] Alembic 0001 handwritten (enums, tables, constraints, trigger). Test: `alembic upgrade head` on fresh schema + insert/query round-trips; CHECK violations raise; trigger bumps `updated_at`.

## Task 3: Domain + ticket service + roles service

**Files:** `app/domain/enums.py`, `app/domain/errors.py`, `app/domain/state_machine.py`, `app/services/users.py`, `app/services/tickets.py`, `app/services/roles.py`, `app/services/deps.py` (session helper), tests: `test_state_machine.py`, `test_tickets_take.py`, `test_tickets_transitions.py`, `test_version_rules.py`, `test_roles.py`, `test_events_journal.py`

- [ ] `state_machine.py`: pure transition table `TRANSITIONS: dict[TicketStatus, set[Action]]`; actions `take, to_waiting, to_progress, resolve, reject, duplicate, abandon, reopen, reassign`; helpers `is_final`, `entering_new`, `leaving_new`.
- [ ] Errors: `NotFound, Forbidden, Conflict(msg), VersionConflict(stale|already_taken), RoleChangeBlocked(active_tickets), InvalidTransition`.
- [ ] `users.ensure_user(session, telegram_id, username, full_name)` upsert profile fields each interaction; `assign_bootstrap_role` on first /start per BOOTSTRAP_ADMIN_IDS.
- [ ] All business mutations: single conditional UPDATE with `WHERE id=:id AND version=:ev AND status=:es`, `RETURNING`, then event row in same tx; `version+1` exactly once per business change. Permissions checked in same tx (role re-read from DB — never cached). Take/reassign lock assignee row `FOR SHARE`.
- [ ] `take`: new→in_progress, assignee=actor, taken_at=now(), next_reminder_at=NULL; conflict→classify stale vs already_taken by re-read. Also observes time-to-take histogram from journal.
- [ ] Status actions per state machine; `abandon`:→new resets assignee/taken_at, next_reminder_at=now()+interval; finals set closed_at=now(); reopen (admin)→new resets all; reassign (admin, in_progress|waiting_info, target must hold developer/admin role).
- [ ] `roles.set_role`: `FOR UPDATE` user row; block demotion from executor-capable role if user has in_progress|waiting_info tickets — error lists them.
- [ ] Metrics calls (counters) invoked here but exported by Task 8 module (import-safe no-ops until defined).
- [ ] Tests (against real PG): full matrix legal/illegal transitions incl. admin-as-developer, new→final forbidden, reopen/reassign admin-only; version increments: 1 transition=+1, technical writes (prompt/save message_id/move reminder) don't; take atomicity (two concurrent takes → exactly one wins, second gets already_taken); stale button after in_progress→waiting_info→in_progress cycle rejected (version 3 vs 1); role-change guard blocks with active tickets, allowed after reassign; FOR SHARE vs FOR UPDATE race: concurrent take + demotion never yields active ticket on roleless user; journal rows correct (old/new status/assignee, version_after); CHECK invariants hold after every action path.

## Task 4: Post rendering + sync service + sweeper

**Files:** `app/telegram_gateway.py`, `app/bot/render.py`, `app/services/post_sync.py`, `app/workers/post_sync_worker.py`, `tests/test_render.py`, `tests/test_post_sync.py`

- [ ] Gateway: thin wrapper over PTB `Bot` — `send_message`/`edit_message_text` with backoff honoring `retry_after`, "message is not modified" → success, returns message_id; `FakeGateway` in tests records calls + scripted failures.
- [ ] `render.py`: post text = `#№ / статус / приоритет / заявка / арендатор / автор / исполнитель / описание`, description truncated with `…` so total ≤4096 (assert + test with 3000-char unicode); markup by status: `new→[Взять]`; taken→`[В работу|Ожидание][Решено|Отклонено|Дубль][Отказаться]`; final→no buttons (reopen is Mini-App only). Callback data `t:<act>:<id>:<ver>`.
- [ ] `post_sync.sync_ticket(ticket_id)`: pinned dedicated connection; `pg_try_advisory_lock` with timeout, retry loop; under lock re-read + render (never cached text); absent `message_id`→send, else edit; failure of edit due deleted post→send new; save `post_synced_version=<rendered version>` (+`message_id` for new post) one tx, version untouched; `finally pg_advisory_unlock`; unlock failure → `CONNECT`-discard connection (close, don't return to pool); task cancellation-safe (unlock runs in finally under shield).
- [ ] `sync_ticket_forced(ticket_id)` («Восстановить пост»): publish new post regardless, replace message_id.
- [ ] Sweeper worker: each interval `SELECT id FROM tickets WHERE message_id IS NULL OR post_synced_version<version` → `sync_ticket` via try-lock (busy→skip).
- [ ] Tests: sync idempotent → not re-picked after success; concurrent edits (interleave edit→re-render) never post stale; change during send → post_synced_version<version → sweeper re-syncs; ticket with no post published by sweeper; try-lock skips when other holder busy; lock released on exception and on CancelledError; fake message_delete → next sync re-publishes; render ≤4096; "not modified" treated as success; retry_after respected (fake clock).

## Task 5: Bot — webhook/polling, dedup, commands, draft form, buttons

**Files:** `app/bot/application.py`, `app/bot/handlers/common.py`, `app/bot/handlers/create_form.py`, `app/bot/handlers/ticket_actions.py`, `app/services/drafts.py`, `app/services/dedup.py`, `app/api/webhook.py`, `app/polling.py`, `tests/test_dedup.py`, `tests/test_drafts.py`, `tests/test_form_flow.py`, `tests/test_ticket_actions_handlers.py`

- [ ] PTB `Application` (no Updater) built in lifespan; error handler → Sentry with tags. `POST /telegram/webhook`: constant-time compare `X-Telegram-Bot-Api-Secret-Token`; `dedup.reserve(update_id)` short tx first (INSERT ON CONFLICT; duplicate→skip); `await asyncio.wait_for(app.process_update(u), PROCESS_UPDATE_TIMEOUT)`; exceptions logged→Sentry; always 200.
- [ ] `polling.py` loop (BOT_MODE=polling): `get_updates` → same dedup+process path → offset. Cleanup task: processed_updates >48h, drafts >TTL.
- [ ] Commands: `/start` (ensure_user, bootstrap admin or «обратитесь к админу», menu button registration via set_my_commands with WebAppButton), `/help`, `/cancel` (delete draft), `/new` (continue/restart prompt if active draft).
- [ ] `drafts.py` service: create(uuid4, step=description, version=1); ≤1 active/user (unique idx); text steps bind `reply_to_message_id == prompt_message_id` else re-ask without touching data/step/version; accepted answer: one tx `SELECT FOR UPDATE` save+advance+clear prompt+version+1, after commit send next question, save prompt id conditionally `WHERE id AND version AND step`; priority buttons skip/confirm/cancel carry `d:<hex32>:<act>:<ver>` — stale/foreign rejected with alert «продолжите с актуального сообщения»; submit: one tx INSERT ticket + DELETE draft, reply `#№`, post_sync after commit.
- [ ] `ticket_actions.py`: parse `t:`, resolve role from DB, call service, on VersionConflict alert («Обращение изменилось» / «Уже взято»), trigger post_sync, answer(callback) OK; button press on ticket not in dev-chat-visible state just syncs post.
- [ ] Tests: reserve once / second insert returns False; processing error doesn't roll back reserve row; draft concurrency (two callbacks different replicas order via FOR UPDATE — both applied, version monotonic), foreign/old-version callback rejected without data change, resubmitted answer after failure can't leak into next field (prompt binding), late question send doesn't overwrite newer prompt (conditional update no-op), full happy path → ticket created + draft gone + double-confirm creates nothing, button take: success for developer, alert for support, «Уже взято» race, «Отказаться» resets to new.

## Task 6: Reminders

**Files:** `app/services/reminders.py`, `app/workers/reminder_worker.py`, `tests/test_reminders.py`

- [ ] Claim step in one tx: `SELECT ... WHERE status='new' AND next_reminder_at<=now() ... FOR UPDATE SKIP LOCKED LIMIT n` → `next_reminder_at=now()+interval(priority)` (technical, version untouched, no journal) → COMMIT; then send reply-to-post via gateway (no reply if post absent); send errors → log+Sentry, no retry (≤1 attempt per due).
- [ ] Schedule invariants already in Task 3 (enter new sets, leave clears) — assert in tests.
- [ ] Worker loop interval from config.
- [ ] Tests: entry to new sets due=now()+interval per priority; leaving clears; two concurrent workers never double-claim (SKIP LOCKED); after simulated sleep past multiple intervals exactly one send, next due anchored at claim time (no catch-up storm); message text `⏰ #№ не взято N — приоритет X`; reply uses message_id from snapshot.

## Task 7: REST API + Mini App

**Files:** `app/api/auth.py`, `app/api/tickets.py`, `app/api/users.py`, `app/api/errors.py`, `app/static/mini_app/{index.html,app.js,api.js,styles.css}`, `tests/test_auth.py`, `tests/test_api_tickets.py`, `tests/test_api_users.py`

- [ ] initData validation per Telegram spec: parse `tg_data`, `hash` removed; secret=HMAC_SHA256(key="WebAppData",msg=bot_token); data_check_string sorted `k=v\n`; hash match; `auth_date` freshness ≤ threshold; role from DB; 401/403 mapping. Test helper builds valid/invalid signatures.
- [ ] Endpoints: `GET /api/tickets?scope=mine|all&status=&priority=&page=&page_size=` (mine: support→author, developer→assignee, admin→both tabs via scope param `mine_created|mine_assigned`), `GET /api/tickets/{id}` full card, admin: `POST /api/tickets/{id}/reopen|reassign|restore_post` (all take `expected_version`; reopen/reassign bump version → post_sync after; restore_post doesn't), `GET /api/users?q=`, `PUT /api/users/{id}/role` (RoleChangeBlocked → 409 with active ticket list).
- [ ] Vanilla JS app: tabs per role, filters (status/priority), pagination, sorting by created desc, card view w/ full description, admin actions incl. reassign picker (developers/admins only), roles screen, light/dark theme via `Telegram.WebApp.themeParams/colorScheme`.
- [ ] Tests: auth matrix (tampered hash, expired auth_date, missing role→403, bootstrap admin), list scoping/filters/pagination, card fields, version-guarded admin actions incl. stale→409 «Обращение изменилось», restore_post sets message_id without version bump, role change blocked/allowed responses.

## Task 8: Monitoring

**Files:** `app/metrics.py`, `tests/test_metrics.py`, plus counter/histogram calls across services (wired in Task 3–7 via `app/metrics.py` import-safe module)

- [ ] `make_asgi_app` served by second uvicorn in lifespan on METRICS_PORT; middleware: `http_requests_total{method,route,status}`, `http_request_duration_seconds{method,route}` with route *template* labels.
- [ ] Counters: `tickets_created_total{priority}`, `ticket_transitions_total{from_status,to_status}`, `updates_processed_total`, `updates_deduplicated_total`, `reminders_sent_total{priority}`, `post_sync_total{result}`, `app_errors_total{component}`; histogram `ticket_time_to_take_seconds{priority}`; custom DB collector gauge `open_tickets_by_status{status}` (scrape-time query, aggregate `max()` across pods — README rule).
- [ ] Tests: middleware labels templated route (/api/tickets/{id} not /api/tickets/12); counters increment through service calls; gauge reflects DB.

## Task 9: Prod image, README, final QA

- [ ] Verify `prod` target builds: non-root, single uvicorn, both ports; `docker compose config` valid.
- [ ] README: local dev (compose up, .env, BotFather notes), env vars table, migrations (dev auto / prod job command), image builds, webhook registration curl with secret token, Mini App setup, metrics aggregation rules (sum vs max for open-tickets gauge).
- [ ] Full gate: `ruff check` + `ruff format --check` + `mypy app` + full `pytest` all green; grep repo for committed secrets (none); `.env` untracked.
