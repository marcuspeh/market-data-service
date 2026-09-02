"""Shared fixtures and helpers for the unit-test suite."""
from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

import app.config.settings as settings_module


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wipe env vars so settings is hermetic; tests set values explicitly."""
    for key in list(os.environ):
        if key.startswith(("POLYGON_", "LONGBRIDGE_", "APP_", "DATA_")):
            monkeypatch.delenv(key, raising=False)

    hermetic_settings = settings_module.Settings(_env_file=None)
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


@dataclass
class FakeSettings:
    """Lightweight stand-in for Settings — only the attributes our code touches."""

    polygon_api_key: str = "test-key"
    polygon_base_url: str = "https://api.polygon.io"
    longbridge_timeout_seconds: float = 5.0
    longbridge_region_suffix: str = ".US"


@pytest.fixture
def fake_settings() -> FakeSettings:
    return FakeSettings()