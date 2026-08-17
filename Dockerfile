# syntax=docker/dockerfile:1.7
#
# Multi-stage build for market-data-service.
#
# Stage 1 (builder): use uv to resolve and install the locked dependencies
#                    into a virtual environment that we copy wholesale.
# Stage 2 (runtime): slim Python image with the venv from the builder, the
#                    application source, and a non-root user.

# ---------- builder ----------
FROM python:3.13-slim AS builder

# uv ships a static binary; pinning the version keeps builds reproducible.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /usr/local/bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install dependencies first (separate layer for cache hits).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# Copy the source and install the project itself (no dev deps in runtime).
COPY . /app
RUN uv sync --frozen --no-dev

# ---------- runtime ----------
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}"

# Create a non-root user and chown the app dir so the user can write the
# .venv into /app (uv's default).
RUN groupadd --system app && useradd --system --gid app --home /app app \
    && mkdir -p /app && chown -R app:app /app

WORKDIR /app
USER app

# Copy the resolved venv and the application source.
COPY --from=builder --chown=app:app /app /app

EXPOSE 3556

# Use uvicorn directly for a stable, production-grade ASGI runner.
# Port 3556 must match the in-container port in docker-compose.yml's
# `${APP_PORT:-8001}:3556` mapping.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3556"]