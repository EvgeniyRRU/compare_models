# syntax=docker/dockerfile:1

FROM python:3.14-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app


# --- dev: runtime + dev deps, hot reload via bind mount ---
FROM base AS dev

COPY pyproject.toml ./
COPY app ./app
RUN pip install -e ".[dev]"
COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]


# --- test: runtime + test deps, run pytest ---
FROM base AS test

COPY pyproject.toml ./
COPY app ./app
COPY tests ./tests
RUN pip install -e ".[test]"
COPY . .

CMD ["pytest"]


# --- prod: runtime deps only (minimal, finalized in a later stage) ---
FROM base AS prod

COPY pyproject.toml ./
COPY app ./app
RUN pip install -e "."
COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
