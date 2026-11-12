FROM python:3.14-slim AS base
WORKDIR /app
COPY pyproject.toml README.md ./
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
RUN pip install --no-cache-dir .

FROM base AS dev
RUN pip install --no-cache-dir '.[dev]'
CMD ["sh", "-c", "exec uvicorn app.main:app --host \"${HOST:-0.0.0.0}\" --port \"${PORT:-8000}\" --reload"]

FROM dev AS test
COPY tests ./tests
CMD ["sh", "-c", "alembic upgrade head && python -m pytest -q"]

FROM base AS prod
RUN useradd --create-home appuser
USER appuser
EXPOSE 8000 9090
CMD ["sh", "-c", "exec uvicorn app.main:app --host \"${HOST:-0.0.0.0}\" --port \"${PORT:-8000}\""]
