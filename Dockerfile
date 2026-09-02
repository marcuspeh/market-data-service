# syntax=docker/dockerfile:1.7
#
# Unified image for market-data-service.
#
# Plain Ubuntu 24.04 base with Python 3.12 + uv + the app on top.
# The app no longer talks to IB Gateway — today's bar comes from
# Longbridge's HTTP / WebSocket endpoints over the public internet.

FROM ubuntu:24.04 AS base

USER root
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON=python3.12 \
    TZ=America/New_York

RUN apt-get update -y \
 && apt-get install --no-install-recommends --yes \
        python3.12 python3.12-venv python3.12-dev \
        build-essential ca-certificates curl tzdata \
 && ln -sf /usr/bin/python3.12 /usr/bin/python3 \
 && ln -sf /usr/bin/python3.12 /usr/bin/python \
 && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
 && echo $TZ > /etc/timezone \
 && curl -sSfL https://github.com/astral-sh/uv/releases/download/0.5.11/uv-installer.sh \
    | env UV_UNMANAGED_INSTALL=/usr/local/bin sh \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 --gid 1000 appuser
USER 1000:1000
WORKDIR /home/appuser/app

ENV PATH="/home/appuser/app/.venv/bin:${PATH}" \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/home/appuser/app/.venv \
    PYTHONPATH=/home/appuser/app

# ---------- builder ----------
FROM base AS builder

COPY --chown=1000:1000 pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY --chown=1000:1000 . /home/appuser/app
RUN uv sync --frozen --no-dev

# ---------- runtime ----------
FROM base AS runtime

COPY --chown=1000:1000 --from=builder /home/appuser/app /home/appuser/app

# In-container port matches docker-compose.yml's APP_PORT mapping.
EXPOSE 8001

ENV PORT_API=8001

ENTRYPOINT ["/home/appuser/app/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]