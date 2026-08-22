# syntax=docker/dockerfile:1.7
#
# Unified image for market-data-service.
#
# Layered on top of gnzsnz/ib-gateway, which already brings:
#   - Ubuntu 24.04 with Xvfb, IBC, socat, sshpass, sudo
#   - IB Gateway binary + IBC bootstrap scripts under /home/ibgateway
#   - socat that forwards 127.0.0.1:4004 -> 127.0.0.1:4002 (paper)
#
# We only need to add Python + the market-data-service app on top.
# `docker-entrypoint.sh` waits for the API socket (4004) and then
# execs uvicorn, so both IB Gateway and the FastAPI app live in
# the same container but stay loosely coupled.

ARG IB_GATEWAY_IMAGE=ghcr.io/gnzsnz/ib-gateway:latest

FROM ${IB_GATEWAY_IMAGE} AS base

# Install Python 3.13 plus the small toolchain the project needs.
# uv is pinned for reproducibility.
USER root
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update -y \
 && apt-get install --no-install-recommends --yes \
        python3.13 python3.13-venv python3.13-dev \
        build-essential ca-certificates curl \
 && ln -sf /usr/bin/python3.13 /usr/bin/python3 \
 && ln -sf /usr/bin/python3.13 /usr/bin/python \
 && curl -sSfL https://github.com/astral-sh/uv/releases/download/0.5.11/uv-installer.sh \
    | env UV_UNMANAGED_INSTALL=/usr/local/bin sh \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# Drop back to the ibgateway user so the app layer matches upstream's
# permission model (the home directory is writable).
USER 1000:1000
WORKDIR /home/ibgateway/app

ENV PATH="/home/ibgateway/app/.venv/bin:${PATH}" \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/home/ibgateway/app/.venv \
    PYTHONPATH=/home/ibgateway/app

# ---------- builder ----------
FROM base AS builder

COPY --chown=1000:1000 pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY --chown=1000:1000 . /home/ibgateway/app
RUN uv sync --frozen --no-dev

# ---------- runtime ----------
FROM base AS runtime

COPY --chown=1000:1000 --from=builder /home/ibgateway/app /home/ibgateway/app

# Entrypoint waits for the paper API socket then launches uvicorn.
COPY --chown=1000:1000 docker-entrypoint.sh /home/ibgateway/app/docker-entrypoint.sh
RUN chmod +x /home/ibgateway/app/docker-entrypoint.sh

# In-container port must match docker-compose.yml's APP_PORT mapping.
EXPOSE 8001 4004

ENV PORT_API=8001 \
    PORT_IB_API=4004 \
    IB_API_READY_TIMEOUT=180

ENTRYPOINT ["/home/ibgateway/app/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]