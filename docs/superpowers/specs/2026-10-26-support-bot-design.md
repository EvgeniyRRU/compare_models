# Support bot: proposed design and implementation stages

Status: approved by the user on 2026-10-26 and implemented. `SPEC.md` remains
the source of requirements. Approval satisfied «Порядок работы», item 1.

## Approach

Use one Python 3.14 application containing FastAPI, an asynchronous PTB
Application, and database-backed background workers. PostgreSQL is the source
of truth for tickets, permissions, drafts, reminder schedules, and update
deduplication. The Mini App uses vanilla JavaScript and the Telegram WebApp SDK.
Each pod runs one uvicorn process; pods coordinate through PostgreSQL.

This is the recommended approach because it follows the specified stack and
keeps deployment and transaction boundaries explicit. A separate worker service
would add deployment components without removing the need for database locks.
A frontend framework would add a build pipeline without a requirement that
needs one. Neither alternative is proposed for the initial implementation.

## Python 3.14 compatibility check

Checked on 2026-10-26 with the existing Python 3.14.2 virtual environment:

- Read PyPI release metadata for all 47 installed third-party distributions.
  Every distribution has a wheel compatible with CPython 3.14 and at least one
  of the checked Linux manylinux x86_64/aarch64 platforms, or a universal wheel.
- Successfully imported FastAPI, PTB, SQLAlchemy asyncio, asyncpg, Alembic,
  pydantic-settings, structlog, Sentry FastAPI/SQLAlchemy integrations,
  prometheus_client, uvicorn, and pytest-asyncio.
- Successfully created and disposed an async SQLAlchemy engine using asyncpg.
  This did not connect to a database.

Candidate direct dependency versions already present locally:

| Dependency | Version |
| --- | --- |
| FastAPI | 0.162.0 |
| python-telegram-bot | 22.8 |
| SQLAlchemy | 2.1.6 |
| asyncpg | 0.32.0 |
| Alembic | 1.20.1 |
| pydantic-settings | 2.16.1 |
| uvicorn | 0.54.0 |
| structlog | 26.1.0 |
| sentry-sdk | 2.73.0 |
| prometheus_client | 0.26.0 |
| pytest / pytest-asyncio | 9.1.1 / 1.4.0 |
| ruff / mypy | 0.17.2 / 2.4.0 |

Primary sources: [PyPI release metadata](https://docs.pypi.org/api/json/),
[asyncpg](https://pypi.org/project/asyncpg/0.32.0/),
[SQLAlchemy](https://pypi.org/project/SQLAlchemy/2.1.6/), and
[python-telegram-bot](https://pypi.org/project/python-telegram-bot/22.8/).
No blocker was observed in these checks. Full dependency resolution, Linux
container execution, and PostgreSQL integration tests remain implementation
checks; imports and wheel availability alone do not establish full compatibility.

## Project structure

```text
pyproject.toml                   # Python version, dependencies, quality tools
uv.lock                         # Reproducible dependency resolution
.env.example                    # All settings, comments, no real secrets
src/support_bot/
  config.py                     # Validated pydantic-settings configuration
  main.py                       # App factory, lifespan, worker supervision
  db/
    base.py                     # Declarative base and session factories
    models.py                   # Five tables and constraints
    locks.py                    # Physical-connection advisory lock lifecycle
  domain/
    enums.py                    # Roles, priorities, statuses, draft steps
    errors.py                   # Transport-independent business errors
    transitions.py              # Explicit state machine and invariants
  services/
    users.py                    # Profile updates, bootstrap, role changes
    tickets.py                  # Atomic mutations and audit events
    drafts.py                   # Locked draft changes and prompt association
    updates.py                  # Independently committed deduplication
    posts.py                    # Serialized rendering/publication workflow
    reminders.py                # Due-time claims and reminder delivery
  telegram/
    application.py              # PTB setup and shared update entry point
    handlers.py                 # Commands, text replies, callbacks
    rendering.py                # Russian prompts, posts, compact callbacks
    transport.py                # Telegram calls, bounded retries/backoff
  api/
    auth.py                     # initData verification and current DB role
    schemas.py                  # Request and response validation
    tickets.py                  # Lists, details, admin ticket actions
    users.py                    # Admin role management
    health.py                   # Liveness and database readiness
  observability/
    logging.py                  # Context variables and stdout logging
    sentry.py                   # Optional SDK and PTB error reporting
    metrics.py                  # Metrics, middleware, DB-backed collector
  workers.py                    # Reconciliation, reminders, TTL cleanup
  static/
    index.html
    app.js
    styles.css
alembic.ini
migrations/env.py
migrations/versions/0001_initial.py
tests/unit/                     # State machine, auth, rendering, validation
tests/integration/              # Actual PostgreSQL and concurrent sessions
tests/conftest.py
Dockerfile                      # dev, test, prod targets
docker-compose.yml              # Local polling and PostgreSQL
docker-compose.test.yml         # Isolated PostgreSQL test environment
README.md
```

Services own business rules and transaction boundaries. Telegram handlers and
HTTP endpoints translate input, call services, and translate results. Services
accept a transport interface for publication so tests can control Telegram
delays and failures without a live bot. No HTTP or Telegram call holds a
business transaction open.

## Database schema

Use PostgreSQL BIGINT identity keys for users and tickets, BIGINT for Telegram
identifiers, UUID for drafts, and TIMESTAMPTZ for times. Enums use text columns
with named CHECK constraints. Timestamps use database defaults. A shared
PostgreSQL BEFORE UPDATE trigger updates `updated_at` on every table that has
that column, including technical writes.

| Table | Fields and constraints |
| --- | --- |
| `users` | `id` PK; unique `telegram_id`; nullable `username`; `first_name`, nullable `last_name`; nullable checked `role`; `created_at`, `updated_at` |
| `tickets` | `id` PK and public ticket number; `author_id` FK; nullable `assignee_id` FK; nonblank `description` up to 3000 characters; checked `priority`; nonblank `request_id` up to 64 characters; nullable nonblank `tenant_id` up to 64 characters; checked `status`; `version` default 1; `created_at`, `updated_at`; nullable `taken_at`, `closed_at`, `next_reminder_at`, `message_id`; `post_synced_version` default 0 |
| `ticket_events` | identity `id` PK; `ticket_id`, `actor_id` FKs; checked event type; `created_at`; nullable old/new assignee FKs and old status; new status; resulting `version`; unique (`ticket_id`, `version`) |
| `ticket_drafts` | UUID `id` PK; unique `user_id` FK; checked `step`; nullable validated form fields; `version` default 1; nullable `prompt_message_id`; `updated_at` |
| `processed_updates` | `update_id` BIGINT PK; `created_at` for 48-hour retention |

Ticket CHECK constraints enforce positive versions, valid sync versions,
assignment/taken-time invariants for `new` and active statuses, and `closed_at`
being present exactly for final statuses. New tickets have reminder deadlines;
other statuses have no reminder deadline. Final tickets retain their last
assignee and taken time until reopening clears them.

Indexes support author/date and assignee/date lists, status/priority/date
filters, overdue new-ticket scans, unsynchronized post scans, draft TTL cleanup,
processed-update retention, and ticket-event history. List ordering uses
`created_at DESC, id DESC` for stable pagination.

## Transaction and concurrency design

- Every business mutation of an existing ticket uses one UPDATE with expected
  ID, status, and version, increments version once, and inserts one event in
  the same transaction. A conflict rolls back the operation and requests post
  reconciliation. A losing take reports «Уже взято».
- Role changes lock the target user FOR UPDATE and reject demotion with the
  active ticket IDs. Taking/reassigning locks the destination user FOR SHARE,
  then checks their current role. Current actor permissions are read in the
  mutation transaction. Bootstrap promotion happens only on initial `/start`
  registration; later profile upserts never overwrite assigned roles.
- A draft mutation locks the draft FOR UPDATE and validates its UUID, version,
  step, and reply association. Accepted data/step changes increment once and
  clear the prompt ID. Prompt sends happen after commit; storing the returned
  ID checks the original UUID/version/step. Submission inserts a ticket and
  event and deletes the draft atomically. Starting/restarting drafts also
  serializes on the owning user to enforce one draft per user.
- A dedicated physical PostgreSQL connection holds the session advisory lock
  for each post. Rendering always rereads committed state under that lock.
  After Telegram succeeds, save the rendered version and message ID in a short
  transaction. Cancellation-safe cleanup explicitly unlocks; failure to unlock
  invalidates/closes the connection. Background reconciliation uses try-locks.
- Reminder workers claim due rows with FOR UPDATE SKIP LOCKED, advance deadlines
  from the current time, and commit before making one send attempt per claim.
- Webhook and polling both claim `update_id` through a separately committed
  INSERT ON CONFLICT DO NOTHING before `process_update`. Errors do not roll
  back deduplication. Retention/TTL workers operate in bounded batches.

## API and Mini App

All `/api` routes verify raw Telegram initData, HMAC, auth-date freshness, and
the current database role. Profile data is refreshed from verified Telegram
identity. Reject duplicate authentication keys, malformed input, future dates
outside a small configured clock tolerance, and expired data.

| Endpoint | Behavior |
| --- | --- |
| `GET /api/me` | Current identity, role, available personal views |
| `GET /api/tickets` | `scope=all/created/assigned`, filters, bounded pagination |
| `GET /api/tickets/{id}` | Full ticket and permitted admin actions |
| `POST /api/tickets/{id}/reopen` | Admin; expected status and version |
| `POST /api/tickets/{id}/reassign` | Admin; expected status/version and destination |
| `POST /api/tickets/{id}/restore-post` | Admin; technical force-publication |
| `GET /api/users` | Admin; paginated users, optionally eligible assignees |
| `PATCH /api/users/{id}/role` | Admin; nullable role, active-ticket conflict list |

The Russian interface has personal/all-ticket views, status and priority
filters, pagination, ticket details, and admin controls. Admin has separate
created/assigned tabs. Theme colors follow Telegram and react to theme changes.
Regular status actions stay in the bot. User-provided text is rendered as text
in the browser and escaped for Telegram formatting.

## Implementation stages and acceptance checks

1. **Scaffold and configuration.** Add packaging, lockfile, settings, settings
   validation tests, and test fixtures. Require Python 3.14; validate webhook
   secrets and mode-specific settings. Verify Linux wheel resolution.
2. **Models and migrations.** Create all five tables, triggers, constraints,
   and indexes. Run Alembic upgrade/downgrade/upgrade on real PostgreSQL; test
   invalid rows and automatic timestamps.
3. **State machine and services.** Write failing transition/permission tests,
   implement ticket/user services and event insertion, then run simultaneous
   take and role-change tests with independent database sessions. Cover stale
   versions after a status cycle and exactly one increment/event per change.
4. **Post synchronization.** Implement rendering, transport retries including
   RetryAfter, dedicated-connection locks, and reconciliation. Test concurrent
   edits, changes during send, missing/deleted posts, no repeated successful
   reconciliation, cancellation/error lock release, and the 4096-character
   Telegram limit (including supplementary Unicode characters).
5. **Bot.** Implement `/start`, `/help`, `/new`, `/cancel`, persisted form,
   ForceReply prompts, confirmation, compact callbacks, and private-chat
   Mini App menu. Test concurrent replies, old/replaced drafts, delayed prompt
   saves, retried text, validation, and atomic submission. Add shared committed
   deduplication and test failures/duplicates in webhook and polling paths.
6. **Reminders.** Implement claiming, delivery, and cleanup loops. Test entry
   into/exit from `new`, preserved ticket versions, no catch-up burst, and no
   duplicate claims between workers.
7. **API and Mini App.** Implement HMAC authentication, current-role checks,
   lists/details, admin actions, and static UI. Test invalid/expired initData,
   access changes, pagination/filtering, conflicts, and technical post restore.
8. **Logging and monitoring.** Add context-tagged logs, optional Sentry,
   template-path HTTP metrics, business metrics, and a DB-backed custom gauge.
   Serve metrics on a separate port; test that the public app has no metrics
   route and that readiness detects database failures.
9. **Docker.** Add dev/test/prod targets, non-root single-process production,
   local polling compose, and isolated integration-test compose. Verify image
   builds and execute the PostgreSQL-backed suite in the test target.
10. **README and final verification.** Document local setup, every setting,
    migrations, BotFather/webhook setup, deployment boundaries, failure
    semantics, and Prometheus `sum()` versus `max()` aggregation. Run ruff
    lint/format checks, mypy, the full test suite, and container smoke checks.

Each stage includes its relevant tests before implementation. No live Telegram
messages or deployments are needed for automated verification. If live
credentials are unavailable, document that live delivery was not exercised.

## Decisions included in this proposal

- Adopt all defaults marked «допущение» in `SPEC.md`: vanilla JS, the four
  priorities, unformatted external IDs, executor/admin refusal, 24-hour drafts,
  reminder intervals 10 minutes / 30 minutes / 2 hours / 8 hours, and structlog.
- Use database triggers consistently for `updated_at`.
- Use PostgreSQL 18 for local development and test containers.
- Use `uv` and a committed lockfile; plain pip installation remains documented.
- Webhook configuration is an explicit operator command, not an automatic
  startup action performed by every pod. Polling is a local single-replica
  mode; it uses the same deduplication and business services as webhook mode.
- No ticket-content editing endpoint is added: the spec defines no editing
  flow. Every implemented status/assignment mutation still follows its version
  rules. Future editing must use those same rules.

Implementation adds a nullable `users.started_at` timestamp so bootstrap promotion
happens exactly on the first `/start`, even if a prior interaction already
registered the profile. This marker also prevents later `/start` commands from
restoring a revoked administrator role.

Verified implementation: all 166 tests pass against PostgreSQL in the isolated
Compose test environment. This includes actual PTB dispatch across two simulated
replicas, cancellation-safe post locks, concurrent mutations and drafts,
DB-backed metrics, API authentication, and retention cleanup. Ruff lint/format,
mypy, and JavaScript syntax checks pass; the Mini App passed headless browser
smoke checks for all roles and self-demotion. Test and production Docker images
build successfully. Live Telegram delivery and deployment require operator
credentials and infrastructure.
