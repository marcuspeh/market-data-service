"""Tests for the pydantic-settings configuration.

Each test instantiates Settings with ``_env_file=None`` so the local
``.env`` (if present) is ignored — keeps tests hermetic regardless of
the developer's shell environment.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config.settings import Settings


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


class TestIbkrPortResolution:
    def test_paper_trading_mode_returns_paper_port(self):
        s = _settings(ibkr_trading_mode="paper", ibkr_port_paper=4004)
        assert s.ibkr_port == 4004

    def test_live_trading_mode_returns_live_port(self):
        s = _settings(ibkr_trading_mode="live", ibkr_port_live=4003)
        assert s.ibkr_port == 4003

    def test_unknown_mode_defaults_to_paper(self):
        s = _settings(ibkr_trading_mode="weird")
        assert s.ibkr_port == 4004


class TestDataDir:
    def test_market_data_dir_derived(self):
        s = _settings(data_dir="/var/data")
        assert s.market_data_dir == Path("/var/data/market")

    def test_constituents_dir_derived(self):
        s = _settings(data_dir="/var/data")
        assert s.constituents_dir == Path("/var/data/constituents")


class TestEnvOverrides:
    def test_explicit_kwargs_override_defaults(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("POLYGON_API_KEY", "envkey")
        s = _settings()
        assert s.polygon_api_key == "envkey"  # env wins when env_file disabled

    def test_legacy_mysql_fields_are_ignored(self, monkeypatch: pytest.MonkeyPatch):
        """mysql_* env vars from the old config are now unknown and
        silently ignored by pydantic-settings (default behaviour)."""
        monkeypatch.setenv("MYSQL_HOST", "envhost")
        # Should not raise.
        _settings()