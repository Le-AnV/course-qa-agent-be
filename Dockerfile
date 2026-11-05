# syntax=docker/dockerfile:1

# --- Builder: resolve locked dependencies with uv -------------------------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Dependency layers first — re-used until pyproject/uv.lock change
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

# --- Runtime: minimal image, only the venv + application code -------------
FROM python:3.12-slim-bookworm
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer-nogui \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/.venv /app/.venv
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY docker-entrypoint.py ./docker-entrypoint.py

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status == 200 else 1)"

ENTRYPOINT ["python", "docker-entrypoint.py"]
