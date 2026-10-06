"""FastAPI middleware that logs every incoming request + outgoing response.

Captures:

* the inbound method, path, and correlation id (set by the
  CorrelationIdMiddleware), and emits a single ``info`` line on the way in;
* the response status + duration on the way out, with ``error`` if the
  status is >= 500.

All log calls go through :func:`app.logging_setup.client` so events flow
through the logging-system SDK alongside everything else.
"""

from __future__ import annotations

import time
from typing import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.logging_setup import client

try:
    from loggingsdk import current_log_id
except Exception:  # pragma: no cover - SDK unavailable
    current_log_id = None  # type: ignore[assignment]


def _request_id() -> str:
    return current_log_id() if current_log_id is not None else "unknown"


class AccessLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        log = client()
        request_id = _request_id()
        method = request.method
        path = request.url.path
        started = time.perf_counter()

        log.info(
            "request method=%s path=%s request_id=%s",
            method, path, request_id,
        )

        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            log.error(
                "request failed method=%s path=%s request_id=%s elapsed_ms=%.1f",
                method, path, request_id, elapsed_ms,
            )
            raise

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        status = response.status_code
        template = (
            "request failed method=%s path=%s request_id=%s status=%d elapsed_ms=%.1f"
            if status >= 500
            else "request completed method=%s path=%s request_id=%s status=%d elapsed_ms=%.1f"
        )
        log_fn = log.error if status >= 500 else log.info
        log_fn(
            template,
            method, path, request_id, status, elapsed_ms,
        )
        return response