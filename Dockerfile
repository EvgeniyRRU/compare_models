# syntax=docker/dockerfile:1

FROM python:3.14-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH
COPY --from=ghcr.io/astral-sh/uv:0.9.27 /uv /usr/local/bin/uv
WORKDIR /app

# Dependencies only, so code changes do not invalidate this layer.
FROM base AS deps
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --group dev

# dev: hot reload, the source is mounted over /app by docker-compose.
FROM deps AS dev
COPY . .
RUN uv sync --frozen --group dev
ENV BOT_MODE=polling
EXPOSE 8000 9100
CMD ["uvicorn", "support_bot.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# test: dev dependencies, runs the whole suite against the PostgreSQL from compose.
FROM dev AS test
CMD ["pytest"]

# prod: runtime dependencies only, non-root user, exactly one uvicorn process.
FROM base AS prod
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY alembic.ini ./
COPY migrations ./migrations
COPY src ./src
RUN useradd --uid 10001 --no-create-home --user-group app && chown -R app /app
USER app
ENV PYTHONPATH=/app/src
EXPOSE 8000 9100
CMD ["uvicorn", "support_bot.main:app", "--host", "0.0.0.0", "--port", "8000"]
