"""Logging setup and accessor for the logging-system SDK.

Centralised entry point for every log call in the app. Callers use the
returned :func:`client` (a thin facade over ``loggingsdk.Client``) instead
of stdlib ``logging.getLogger(__name__)`` so all events flow through the
Kafka pipeline configured for this service.

Three safety nets:

* If the SDK can't be imported (e.g. running tests without the sibling
  repo on the Python path), every ``client.info(...)`` call is a no-op.
* If Kafka is unreachable, the SDK's async queue drops the oldest and the
  call still returns immediately.
* Third-party log records (uvicorn, httpx, httpcore) reach Kafka via
  :class:`loggingsdk.LoggingHandler`, which :func:`setup_logging` attaches
  to the root logger.

Note that ``client.info(...)`` is dispatched straight to the producer and
does not go through the root logger, so the app's own events do not
appear on stderr — only third-party ones do. Set ``LOG_DISABLED=1`` to
run with stderr output locally.

Noise reduction (mirrors ``expense_tracker/app/logging_setup.py``):

* The third-party HTTP loggers in :data:`_NOISY_HTTP_LOGGERS` are
  ``disabled = True`` outright — records are never built, and a later
  ``setLevel`` cannot accidentally re-enable the firehose.
* :class:`DropHealthAccessFilter` is installed on every handler so the
  Docker healthcheck probing ``/health`` doesn't spam Kafka. Other paths
  are untouched.
"""

from __future__ import annotations

import logging
import sys
from typing import Optional

try:
    import loggingsdk
    from loggingsdk import Client, LoggingHandler, ParseLevel
except Exception:  # pragma: no cover - SDK unavailable
    loggingsdk = None  # type: ignore[assignment]
    Client = None  # type: ignore[assignment]
    LoggingHandler = None  # type: ignore[assignment]
    ParseLevel = None  # type: ignore[assignment]


from app.config.settings import get_settings


_client_instance: Optional["loggingsdk.Client"] = None
"""The shared loggingsdk.Client. ``None`` when the SDK is unavailable or
``log_disabled`` is set."""


#: Third-party HTTP loggers removed from the pipeline entirely.
#: ``httpx`` emits one INFO line per HTTP round trip ("HTTP Request:
#: POST ...") and ``httpcore`` one per connection event;
#: ``uvicorn.access`` emits one line per inbound request. Setting
#: ``disabled = True`` (rather than a raised level) means records are
#: never built at all, and a later ``setLevel`` cannot accidentally
#: re-enable the firehose. Transport failures are already reported by
#: the callers that catch them (the API clients log errors themselves),
#: so nothing is lost by dropping these loggers outright.
_NOISY_HTTP_LOGGERS = (
    "httpx",
    "httpcore",
    "hpack",
    "h2",
    "uvicorn.access",
)

#: Liveness endpoint served by ``app.main``. Kept here so the access-log
#: filter and the FastAPI route agree on the path.
HEALTH_PATH = "/health"


class _NullClient:
    """Drop-in replacement for ``loggingsdk.Client`` when the SDK is
    unavailable or disabled. Every method is a no-op so callers can use
    ``client.info(...)`` unconditionally.
    """

    @property
    def project(self) -> str:  # pragma: no cover - read for tests
        return ""

    def debug(self, *_args, **_kwargs) -> None: pass
    def info(self, *_args, **_kwargs) -> None: pass
    def warn(self, *_args, **_kwargs) -> None: pass
    def error(self, *_args, **_kwargs) -> None: pass
    def fatal(self, *_args, **_kwargs) -> None: pass
    def close(self, *_args, **_kwargs) -> None: pass


class DropHealthAccessFilter(logging.Filter):
    """Drop log records describing a ``/health`` request.

    The Docker healthcheck (or any uptime probe) hits ``/health``
    repeatedly; without this filter each probe would emit one
    ``uvicorn.access`` line and one ``httpx`` line, so the access log
    is dominated by liveness pings. Other paths are untouched, so
    genuine traffic stays visible.
    """

    #: Loggers whose records describe an HTTP round trip. Only these
    #: are inspected; application logs that merely mention ``/health``
    # (e.g. an error message) are kept.
    _HTTP_LOGGERS = frozenset({
        "uvicorn.access",
        "httpx",
        "httpcore",
    })

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name not in self._HTTP_LOGGERS:
            return True
        return HEALTH_PATH not in record.getMessage()


def _install_filters(handler: logging.Handler) -> None:
    """Attach the health filter to ``handler``.

    Filters must live on the *handler*, not the root logger: stdlib only
    runs a logger's own filters for records logged directly to it, so a
    filter on root never sees records that ``httpx`` or
    ``uvicorn.access`` emit — exactly the ones that leak.
    """
    handler.addFilter(DropHealthAccessFilter())


def setup_logging() -> Optional["loggingsdk.Client"]:
    """Configure stdlib ``logging`` and build the SDK client.

    Returns the constructed :class:`loggingsdk.Client` (or ``None`` if
    the SDK is disabled). Idempotent — calling it twice returns the
    existing client.
    """
    global _client_instance

    settings = get_settings()

    # Stderr first so we always have *some* output, even if the SDK
    # can't be imported or Kafka isn't reachable yet.
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        ),
    )
    _install_filters(stderr_handler)

    root = logging.getLogger()
    root.setLevel(_parse_level(settings.log_level))
    # Hard-disable noisy HTTP loggers before the root level is read by
    # child loggers that haven't been instantiated yet, so first-call
    # records are also dropped.
    for name in _NOISY_HTTP_LOGGERS:
        logging.getLogger(name).disabled = True
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(stderr_handler)

    if settings.log_disabled or loggingsdk is None:
        return None

    if _client_instance is None:
        _client_instance = Client(
            bootstrap=settings.log_kafka_brokers,
            project=settings.log_project,
            topic=settings.log_topic,
            async_capacity=settings.log_async_capacity,
            flush_interval=settings.log_flush_interval,
            min_level=ParseLevel(settings.log_level.upper())[0],
        )

    # The handler is re-checked (not just added once) because the stderr
    # handler above clears root.handlers on every call — a module-level
    # ``client()`` during import can therefore strip a handler installed
    # by an earlier call.
    if not any(isinstance(h, LoggingHandler) for h in root.handlers):
        kafka_handler = LoggingHandler(_client_instance)
        _install_filters(kafka_handler)
        root.addHandler(kafka_handler)
    return _client_instance


def client():
    """Return the shared SDK client (constructing one on first call).

    Falls back to a :class:`_NullClient` when the SDK is disabled or
    unavailable, so call sites never need to guard against ``None``.
    """
    if _client_instance is None:
        setup_logging()
    return _client_instance if _client_instance is not None else _NullClient()


def shutdown_logging() -> None:
    """Flush + close the SDK. Safe to call when the SDK is disabled."""
    global _client_instance
    if _client_instance is not None:
        try:
            _client_instance.close()
        finally:
            _client_instance = None


def _parse_level(name: str) -> int:
    """Map a level name (case-insensitive) to a stdlib level constant."""
    return {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARN": logging.WARNING,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "FATAL": logging.CRITICAL,
        "CRITICAL": logging.CRITICAL,
    }.get(name.upper(), logging.INFO)
