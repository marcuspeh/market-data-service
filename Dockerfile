# syntax=docker/dockerfile:1.7
#
# Unified image for market-data-service.
#
# Single-stage image on python:3.12-slim so we get Python pre-installed
# and no UID-1000 collision with a pre-existing ubuntu user. The app
# talks to Longbridge's HTTP / WebSocket endpoints over the public
# internet; no other services are bundled.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_SYSTEM_PYTHON=1 \
    TZ=America/New_York

WORKDIR /app

# curl is used by the compose healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast Python package management.
RUN pip install --no-cache-dir uv

# Install Python deps first for layer caching.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen

# Copy only this service's source tree.
COPY . /app/

# Drop privileges — match config_store's pattern (no explicit UID; let
# the image assign one). Slim images don't pre-create a UID 1000 user.
RUN useradd --create-home --shell /bin/bash app \
    && chown -R app:app /app
USER app

# In-container port matches docker-compose.yml's APP_PORT mapping.
EXPOSE 8001

ENV PORT_API=8001

CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]