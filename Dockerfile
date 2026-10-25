# syntax=docker/dockerfile:1.7
# Таргеты: dev (hot reload), test (pytest), prod (минимальный образ, non-root).

ARG PYTHON_VERSION=3.14
ARG UV_VERSION=0.9.27

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM python:${PYTHON_VERSION}-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"
WORKDIR /app
COPY --from=uv /uv /usr/local/bin/uv

# --- dev: все группы зависимостей, editable-установка, hot reload -------------------------
FROM base AS dev
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-install-project
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen
EXPOSE 8000 9000
CMD ["uvicorn", "support_bot.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--reload", "--reload-dir", "src"]

# --- test: только тестовые зависимости, по умолчанию запускает pytest ---------------------
FROM base AS test
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-default-groups --group test
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-default-groups --group test
CMD ["pytest", "-q"]

# --- builder: production-зависимости и пакет без dev-групп --------------------------------
FROM base AS builder
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-install-project --no-dev
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable

# --- prod: минимальный образ, non-root, ровно один процесс uvicorn ------------------------
FROM python:${PYTHON_VERSION}-slim AS prod
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --no-create-home app
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
# Миграции запускаются отдельной командой (в k8s — отдельный job): alembic upgrade head
COPY alembic.ini ./
COPY migrations ./migrations
USER 10001:10001
# 8000 — HTTP (webhook, API, Mini App, health); 9000 — /metrics (METRICS_PORT), не для ingress.
EXPOSE 8000 9000
# Один процесс: без --workers и без gunicorn, масштабирование — числом реплик.
CMD ["uvicorn", "support_bot.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--no-access-log"]
