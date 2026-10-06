"""Shared fixtures and helpers for the unit-test suite."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import pytest

import app.config.settings as settings_module
from app import logging_setup as logging_pkg


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wipe env vars so settings is hermetic; tests set values explicitly.

    Also force ``LOG_DISABLED=1`` so :func:`app.logging_setup.setup_logging`
    installs only the stderr handler and skips the Kafka client (mirrors
    config_store/backend's test layout).
    """
    for key in list(os.environ):
        if key.startswith(("POLYGON_", "LONGBRIDGE_", "APP_", "DATA_", "LOG_")):
            monkeypatch.delenv(key, raising=False)

    hermetic_settings = settings_module.Settings(_env_file=None)
    hermetic_settings.log_disabled = True
    settings_module.get_settings.cache_clear()

    def _get_hermetic():
        return hermetic_settings

    for module_name in (
        "app.config.settings",
        "app.services.constituents_scheduler",
        "app.services.constituents_service",
        "app.services.market_data_service",
    ):
        try:
            monkeypatch.setattr(
                f"{module_name}.get_settings", _get_hermetic, raising=False
            )
        except AttributeError:
            pass

    # Reset the cached SDK client + root handlers so setup_logging can be
    # called freely from individual test files without leaking state.
    logging_pkg._client_instance = None
    root_handlers = list(__import__("logging").getLogger().handlers)
    for h in root_handlers:
        __import__("logging").getLogger().removeHandler(h)


@dataclass
class FakeSettings:
    """Lightweight stand-in for Settings — only the attributes our code touches."""

    polygon_api_key: str = "test-key"
    polygon_base_url: str = "https://api.polygon.io"
    app_port: int = 3556
    longbridge_app_key: str = "test-app-key"
    longbridge_app_secret: str = "test-app-secret"
    longbridge_access_token: str = "test-access-token"
    longbridge_timeout_seconds: float = 5.0
    longbridge_region_suffix: str = ".US"


@pytest.fixture
def fake_settings() -> FakeSettings:
    return FakeSettings()