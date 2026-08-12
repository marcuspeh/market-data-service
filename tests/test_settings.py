"""Tests for the pydantic-settings configuration."""

from __future__ import annotations

import pytest

from app.config.settings import Settings


class TestIbkrPortResolution:
    def test_paper_trading_mode_returns_paper_port(self):
        s = Settings(ibkr_trading_mode="paper", ibkr_port_paper=4004)
        assert s.ibkr_port == 4004

    def test_live_trading_mode_returns_live_port(self):
        s = Settings(ibkr_trading_mode="live", ibkr_port_live=4003)
        assert s.ibkr_port == 4003

    def test_unknown_mode_defaults_to_paper(self):
        s = Settings(ibkr_trading_mode="weird")
        # Falls through to the else branch → paper port
        assert s.ibkr_port == 4004


class TestDatabaseUrl:
    def test_basic_url_construction(self):
        s = Settings(
            mysql_host="db.example.com",
            mysql_port=3306,
            mysql_user="alice",
            mysql_password="s3cr3t!",
            mysql_database="market",
        )
        # Password contains '!' and '@' — both must be URL-encoded.
        assert (
            s.database_url
            == "mysql://alice:s3cr3t%21@db.example.com:3306/market"
        )

    def test_password_with_at_sign_is_url_encoded(self):
        s = Settings(
            mysql_host="db",
            mysql_port=3306,
            mysql_user="u",
            mysql_password="p@ss",
            mysql_database="d",
        )
        # The '@' in the password must not be mistaken for the host separator.
        assert "@" not in s.database_url.split("//", 1)[1].rsplit("@", 1)[0]
        assert s.database_url.startswith("mysql://u:p%40ss@db:3306/d")


class TestEnvOverrides:
    def test_env_vars_override_defaults(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MYSQL_HOST", "envhost")
        monkeypatch.setenv("POLYGON_API_KEY", "envkey")
        monkeypatch.setenv("IBKR_TRADING_MODE", "live")
        s = Settings()
        assert s.mysql_host == "envhost"
        assert s.polygon_api_key == "envkey"
        assert s.ibkr_trading_mode == "live"
        assert s.ibkr_port == 4003

    def test_unknown_env_var_is_accepted(self, monkeypatch: pytest.MonkeyPatch):
        """pydantic-settings ignores unknown env vars by default.

        We accept any extra env vars so the service boots in environments
        that have additional variables defined (e.g. CI runners)."""
        monkeypatch.setenv("MARKET_DATA_MAX_DAYS", "30")
        # Should not raise.
        Settings()