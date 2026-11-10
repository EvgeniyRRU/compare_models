FROM python:3.14-slim AS base
WORKDIR /app
COPY pyproject.toml ./

FROM base AS dev
RUN pip install --no-cache-dir '.[dev]'
COPY . .
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

FROM dev AS test
CMD ["pytest"]

FROM base AS prod
RUN pip install --no-cache-dir . && useradd --system --uid 10001 app
COPY --chown=app:app app ./app
COPY --chown=app:app alembic ./alembic
COPY --chown=app:app alembic.ini ./alembic.ini
USER app
EXPOSE 8000 9090
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
