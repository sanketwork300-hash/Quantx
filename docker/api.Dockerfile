# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*

# Dependency layer first so source edits do not invalidate the wheel cache.
# The package directories must exist for an editable install, but they are
# stubbed here and replaced by the real source below, so editing a module does
# not force a full dependency reinstall.
COPY pyproject.toml README.md ./
RUN mkdir -p apps api domains quant infrastructure \
    && touch apps/__init__.py api/__init__.py domains/__init__.py \
             quant/__init__.py infrastructure/__init__.py \
    && pip install --upgrade pip setuptools wheel \
    && pip install -e ".[dev,validation]"

COPY alembic.ini ./
COPY migrations ./migrations
COPY infrastructure ./infrastructure
COPY quant ./quant
COPY domains ./domains
COPY api ./api
COPY apps ./apps
COPY scripts ./scripts

# Provenance depends on knowing which code produced a number, so the commit is
# baked in at build time rather than discovered at runtime (the .git directory
# is deliberately not copied into the image).
ARG GIT_COMMIT=unknown
ENV QIP_CODE_COMMIT=$GIT_COMMIT

# API, worker and scheduler share this image and differ only by command, so a
# task can never drift from the service that enqueues it.
RUN useradd --create-home --uid 10001 qip \
    && mkdir -p /var/qip/objectstore \
    && chown -R qip:qip /app /var/qip
USER qip

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/v1/health || exit 1

CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
