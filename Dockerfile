# syntax=docker/dockerfile:1.7

FROM python:3.14-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app/src

COPY --from=ghcr.io/astral-sh/uv:0.9.27 /uv /uvx /bin/
WORKDIR /app

FROM base AS deps
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project
COPY . .

FROM deps AS dev
CMD ["uvicorn", "support_bot.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

FROM deps AS test
CMD ["pytest"]

FROM base AS prod
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY alembic.ini ./
COPY migrations ./migrations
COPY src ./src
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser
EXPOSE 8000 9100
CMD ["uvicorn", "support_bot.main:app", "--host", "0.0.0.0", "--port", "8000"]