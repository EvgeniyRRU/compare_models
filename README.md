# Rent Support Bot

Telegram bot and Mini App for tracking first line support requests.

## Local development with Docker Compose

1. Copy `.env.example` to `.env` and set `BOT_TOKEN`, `DEV_CHAT_ID`, and at least one `BOOTSTRAP_ADMIN_IDS` Telegram ID.
2. Start the application and PostgreSQL:

   ```sh
   docker compose up --build
   ```

3. In another terminal, run migrations:

   ```sh
   docker compose exec app alembic upgrade head
   ```

The bot uses polling in local development. The API is available at `http://localhost:8000`; liveness and readiness endpoints are `/health/live` and `/health/ready`.

## Configuration

All settings are environment variables documented in `.env.example`. Production secrets should be supplied by the deployment environment. Set `BOT_MODE=webhook` and configure `TELEGRAM_WEBHOOK_SECRET` for webhook operation.

## Docker targets

- `dev`: editable development setup with reload and development tools.
- `test`: runs pytest.
- `prod`: minimal non-root runtime with one Uvicorn process.

Build a target with `docker build --target prod -t rent-support-bot .`.
Run the PostgreSQL integration suite with `docker compose --profile test run --build --rm test`.

## Metrics aggregation

Prometheus process metrics are additive across replicas and should be aggregated with `sum()`. Database-derived open ticket gauges return the same value from each replica and should be aggregated with `max()`. Scrape metrics only through the internal metrics listener.

## Telegram setup

Create a bot with BotFather and configure its token in `BOT_TOKEN`. Add the bot to the developer chat and set `DEV_CHAT_ID`. For webhook production deployments, register the HTTPS webhook URL and its secret token with Telegram. Configure the Mini App URL in BotFather after deploying behind HTTPS.
