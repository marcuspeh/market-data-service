"""Logging setup and accessor for the logging-system SDK.

Centralised entry point for every log call in the app. Callers use the
returned :func:`client` (a thin facade over ``loggingsdk.Client``) instead
of stdlib ``logging.getLogger(__name__)`` so all events flow through the
Kafka pipeline configured for this service.

Two safety nets:

* If the SDK can't be imported (e.g. running tests without the sibling
  repo on the Python path), every ``client.info(...)`` call is a no-op.
* If Kafka is unreachable, the SDK's async queue drops the oldest and the
  call still returns immediately.
"""

from __future__ import annotations

import logging
import sys
from typing import Optional

from app.config.settings import get_settings

try:
    import loggingsdk
    from loggingsdk import Client, LoggingHandler, ParseLevel
except Exception:  # pragma: no cover - SDK unavailable
    loggingsdk = None  # type: ignore[assignment]
    Client = None  # type: ignore[assignment]
    LoggingHandler = None  # type: ignore[assignment]
    ParseLevel = None  # type: ignore[assignment]


_client_instance: Optional["loggingsdk.Client"] = None
"""The shared loggingsdk.Client. ``None`` when the SDK is unavailable or
``log_disabled`` is set."""


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

    root = logging.getLogger()
    root.setLevel(_parse_level(settings.log_level))
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(stderr_handler)

    if settings.log_disabled or loggingsdk is None:
        return None

    if _client_instance is not None:
        return _client_instance

    _client_instance = Client(
        bootstrap=settings.log_kafka_brokers,
        project=settings.log_project,
        topic=settings.log_topic,
        async_capacity=settings.log_async_capacity,
        flush_interval=settings.log_flush_interval,
        min_level=ParseLevel(settings.log_level.upper())[0],
    )
    root.addHandler(LoggingHandler(_client_instance))
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