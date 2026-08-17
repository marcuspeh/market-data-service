"""Shared fixtures and helpers for the unit-test suite."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wipe Polygon/IBKR-related env vars so settings doesn't pick them
    up from the developer's shell during tests. Anything tests actually
    want to set should be set explicitly inside the test.

    Also replaces ``get_settings()`` across the codebase with a hermetic
    instance that ignores the local .env file.
    """
    for key in list(os.environ):
        if key.startswith(("POLYGON_", "IBKR_", "APP_", "DATA_")):
            monkeypatch.delenv(key, raising=False)

    import app.config.settings as settings_module

    hermetic_settings = settings_module.Settings(_env_file=None)
    settings_module.get_settings.cache_clear()

    def _get_hermetic():
        return hermetic_settings

    # Patch every module that may have imported the function at load time.
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


@dataclass
class FakeSettings:
    """Lightweight stand-in for the real Settings class — only the
    attributes our code touches, set to sensible test defaults."""

    polygon_api_key: str = "test-key"
    polygon_base_url: str = "https://api.polygon.io"
    ibkr_host: str = "127.0.0.1"
    ibkr_trading_mode: str = "paper"
    ibkr_port_paper: int = 4004
    ibkr_port_live: int = 4003
    ibkr_client_id: int = 1
    ibkr_timeout_seconds: float = 5.0

    @property
    def ibkr_port(self) -> int:
        return (
            self.ibkr_port_live
            if self.ibkr_trading_mode == "live"
            else self.ibkr_port_paper
        )


@pytest.fixture
def fake_settings() -> FakeSettings:
    return FakeSettings()