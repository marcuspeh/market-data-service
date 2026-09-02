"""Tests for the pydantic-settings configuration."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config.settings import Settings


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


class TestLongbridgeSettings:
    def test_default_credentials_are_empty(self):
        s = _settings()
        assert s.longbridge_app_key == ""
        assert s.longbridge_app_secret == ""
        assert s.longbridge_access_token == ""

    def test_default_timeout(self):
        s = _settings()
        assert s.longbridge_timeout_seconds == 10.0

    def test_default_region_suffix(self):
        s = _settings()
        assert s.longbridge_region_suffix == ".US"

    def test_overrides_take_effect(self):
        s = _settings(
            longbridge_app_key="k",
            longbridge_app_secret="s",
            longbridge_access_token="t",
            longbridge_timeout_seconds=2.5,
            longbridge_region_suffix=".HK",
        )
        assert s.longbridge_app_key == "k"
        assert s.longbridge_app_secret == "s"
        assert s.longbridge_access_token == "t"
        assert s.longbridge_timeout_seconds == 2.5
        assert s.longbridge_region_suffix == ".HK"


class TestDataDir:
    def test_market_data_dir_derived(self):
        s = _settings(data_dir="/var/data")
        assert s.market_data_dir == Path("/var/data/market")

    def test_constituents_dir_derived(self):
        s = _settings(data_dir="/var/data")
        assert s.constituents_dir == Path("/var/data/constituents")


class TestAppPort:
    def test_default(self):
        assert _settings().app_port == 3556

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("APP_PORT", "8000")
        assert _settings().app_port == 8000


class TestEnvOverrides:
    def test_explicit_kwargs_override_defaults(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("POLYGON_API_KEY", "envkey")
        s = _settings()
        assert s.polygon_api_key == "envkey"