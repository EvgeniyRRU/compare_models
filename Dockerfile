FROM python:3.14-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore
WORKDIR /srv/app
RUN addgroup --system app && adduser --system --ingroup app app

FROM base AS deps
COPY pyproject.toml README.md ./
COPY app ./app
COPY tests ./tests
RUN pip install --no-cache-dir -e . --group dev
RUN chown app:app /srv/app

FROM deps AS dev
USER app
EXPOSE 8000 9464
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload"]

FROM deps AS test
USER app
CMD ["pytest"]

FROM base AS prod
COPY pyproject.toml README.md ./
COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations
RUN pip install --no-cache-dir .
USER app
EXPOSE 8000 9464
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
