# syntax=docker/dockerfile:1
FROM python:3.14-slim AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_COMPILE_BYTECODE=0 UV_LINK_MODE=copy
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./

# --- dev: dev-зависимости, hot reload (см. docker-compose.yml) ---
FROM base AS dev
RUN uv sync --frozen --no-install-project --group dev
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app/src
COPY . .
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--reload", "--reload-dir", "src"]

# --- test: тестовые зависимости, по умолчанию запускает pytest ---
FROM base AS test
RUN uv sync --frozen --no-install-project --group test
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app/src
COPY . .
CMD ["pytest"]

# --- prod-сборка зависимостей ---
FROM base AS prod-build
RUN uv sync --frozen --no-install-project --no-dev

# --- prod: минимальный образ, non-root, ровно один uvicorn-процесс ---
FROM python:3.14-slim AS prod
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app/src
RUN useradd --system --uid 10001 --no-create-home app
WORKDIR /app
COPY --from=prod-build /app/.venv /app/.venv
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./
USER 10001
EXPOSE 8000 9100
# Миграции: отдельной командой/job (`alembic upgrade head`), не при старте приложения.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
