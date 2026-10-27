# syntax=docker/dockerfile:1
FROM python:3.14-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.9.27 /uv /bin/uv
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY pyproject.toml uv.lock ./

FROM base AS prod-deps
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable

FROM base AS dev
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --extra dev
EXPOSE 8000 9000
CMD ["uvicorn", "support_bot.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--reload"]

FROM dev AS test
CMD ["pytest", "-q"]

FROM python:3.14-slim AS prod
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/app/.venv/bin:$PATH"
WORKDIR /app
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid 10001 --no-create-home app
COPY --from=prod-deps /app/.venv /app/.venv
COPY migrations ./migrations
COPY alembic.ini ./
USER 10001:10001
EXPOSE 8000 9000
CMD ["python", "-m", "support_bot"]
