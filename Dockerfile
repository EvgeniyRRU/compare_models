# syntax=docker/dockerfile:1

FROM python:3.14-slim AS base
ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

# Install runtime deps first (cached layer). Deps are parsed from pyproject.toml
# with stdlib tomllib — no package build / source needed at this stage.
FROM base AS deps
COPY pyproject.toml ./
RUN python -c "import tomllib; d=tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']; open('/runtime-deps.txt','w').write('\n'.join(d))" \
    && pip install -r /runtime-deps.txt

# ---- dev: hot reload, source bind-mounted by compose ----
FROM deps AS dev
RUN python -c "import tomllib; d=tomllib.load(open('pyproject.toml','rb'))['project']['optional-dependencies']['dev']; open('/dev-deps.txt','w').write('\n'.join(d))" \
    && pip install -r /dev-deps.txt
COPY . .
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# ---- test: runs pytest against real PostgreSQL from compose ----
FROM dev AS test
CMD ["pytest"]

# ---- prod: minimal, non-root, single uvicorn process (scaling via replicas) ----
FROM deps AS prod
COPY app ./app
COPY alembic.ini ./alembic.ini
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser
EXPOSE 8000 9090
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
