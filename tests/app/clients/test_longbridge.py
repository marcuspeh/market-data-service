"""Tests for the LongbridgeClient TTL cache + single-flight behaviour."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.clients.longbridge import LongbridgeClient, LongbridgeError
from app.config.settings import NY_TZ, Settings, ny_now
from tests.conftest import FakeSettings


def _bar(timestamp: int = 1) -> dict[str, Any]:
    return {
        "t": timestamp,
        "o": 100.0,
        "h": 101.0,
        "l": 99.0,
        "c": 100.5,
        "v": 1000.0,
        "vw": 100.2,
        "n": 10,
    }


def _make_client(
    settings: FakeSettings,
    *,
    ttl: float = 60.0,
) -> LongbridgeClient:
    """Build a client with a MagicMocked QuoteContext."""
    client = LongbridgeClient(settings, cache_ttl_seconds=ttl)
    client._ctx = MagicMock()
    return client


class TestCredentials:
    def test_ensure_ctx_passes_settings_credentials_to_config(
        self, fake_settings: FakeSettings
    ):
        client = LongbridgeClient(fake_settings)
        client._ctx = None  # force lazy construction
        with patch("app.clients.longbridge.Config") as config_cls:
            config_cls.from_apikey.return_value = MagicMock()
            with patch("app.clients.longbridge.QuoteContext") as qc:
                ctx = client._ensure_ctx()
        config_cls.from_apikey.assert_called_once_with(
            fake_settings.longbridge_app_key,
            fake_settings.longbridge_app_secret,
            fake_settings.longbridge_access_token,
        )
        qc.assert_called_once_with(config_cls.from_apikey.return_value)
        assert ctx is qc.return_value

    def test_ensure_ctx_is_cached(self, fake_settings: FakeSettings):
        client = LongbridgeClient(fake_settings)
        client._ctx = None
        with patch("app.clients.longbridge.Config") as config_cls:
            config_cls.from_apikey.return_value = MagicMock()
            with patch("app.clients.longbridge.QuoteContext") as qc:
                first = client._ensure_ctx()
                second = client._ensure_ctx()
        assert first is second
        assert config_cls.from_apikey.call_count == 1
        assert qc.call_count == 1


class TestUncachedPath:
    async def test_returns_last_bar(self, fake_settings: FakeSettings):
        client = _make_client(fake_settings)

        async def fake_run(self: LongbridgeClient, ticker: str) -> dict[str, Any]:
            return _bar(timestamp=123)

        client._fetch_today_bar_uncached = fake_run.__get__(client)  # type: ignore[method-assign]
        result = await client.fetch_today_bar("AAPL")
        assert result == _bar(timestamp=123)


class TestTtlCache:
    async def test_second_call_within_ttl_is_served_from_cache(
        self, fake_settings: FakeSettings
    ):
        client = _make_client(fake_settings, ttl=60.0)

        call_count = 0

        async def fake_run(self: LongbridgeClient, ticker: str) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            return _bar(timestamp=call_count)

        client._fetch_today_bar_uncached = fake_run.__get__(client)  # type: ignore[method-assign]

        r1 = await client.fetch_today_bar("AAPL")
        r2 = await client.fetch_today_bar("AAPL")

        assert r1 == r2
        assert call_count == 1

    async def test_cache_expires_after_ttl(self, fake_settings: FakeSettings):
        client = _make_client(fake_settings, ttl=0.05)

        call_count = 0

        async def fake_run(self: LongbridgeClient, ticker: str) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            return _bar(timestamp=call_count)

        client._fetch_today_bar_uncached = fake_run.__get__(client)  # type: ignore[method-assign]

        await client.fetch_today_bar("AAPL")  # call_count = 1
        await asyncio.sleep(0.1)  # > TTL
        await client.fetch_today_bar("AAPL")  # call_count = 2

        assert call_count == 2

    async def test_different_ticker_uses_fresh_entry(
        self, fake_settings: FakeSettings
    ):
        client = _make_client(fake_settings, ttl=60.0)
        call_count = 0

        async def fake_run(self: LongbridgeClient, ticker: str) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            return _bar(timestamp=call_count)

        client._fetch_today_bar_uncached = fake_run.__get__(client)  # type: ignore[method-assign]

        await client.fetch_today_bar("AAPL")
        await client.fetch_today_bar("MSFT")

        assert call_count == 2

    async def test_cached_none_value_is_respected(
        self, fake_settings: FakeSettings
    ):
        """A None result is cached so we don't hammer Longbridge."""
        client = _make_client(fake_settings, ttl=60.0)
        call_count = 0

        async def fake_run(self: LongbridgeClient, ticker: str) -> Any:
            nonlocal call_count
            call_count += 1
            return None

        client._fetch_today_bar_uncached = fake_run.__get__(client)  # type: ignore[method-assign]

        r1 = await client.fetch_today_bar("AAPL")
        r2 = await client.fetch_today_bar("AAPL")

        assert r1 is None
        assert r2 is None
        assert call_count == 1


class TestSingleFlight:
    async def test_concurrent_calls_collapse_to_one_request(
        self, fake_settings: FakeSettings
    ):
        client = _make_client(fake_settings, ttl=60.0)
        active = 0
        max_active = 0
        call_count = 0

        async def fake_run(self: LongbridgeClient, ticker: str) -> dict[str, Any]:
            nonlocal active, max_active, call_count
            active += 1
            max_active = max(max_active, active)
            try:
                await asyncio.sleep(0.05)
                call_count += 1
                return _bar(timestamp=call_count)
            finally:
                active -= 1

        client._fetch_today_bar_uncached = fake_run.__get__(client)  # type: ignore[method-assign]

        results = await asyncio.gather(
            client.fetch_today_bar("AAPL"),
            client.fetch_today_bar("AAPL"),
            client.fetch_today_bar("AAPL"),
        )

        assert all(r == results[0] for r in results)
        assert call_count == 1
        assert max_active == 1, "single-flight should serialize concurrent requests"


class TestErrorPropagation:
    async def test_error_is_raised_to_all_waiters(self, fake_settings: FakeSettings):
        client = _make_client(fake_settings, ttl=60.0)

        async def fake_run(self: LongbridgeClient, ticker: str) -> dict[str, Any]:
            await asyncio.sleep(0.01)
            raise LongbridgeError("boom")

        client._fetch_today_bar_uncached = fake_run.__get__(client)  # type: ignore[method-assign]

        with pytest.raises(LongbridgeError, match="boom"):
            await client.fetch_today_bar("AAPL")

        # Inflight slot must clear so the next call can retry.
        assert client._inflight == {}
        assert client._cache == {}

    async def test_sdk_exception_is_wrapped_in_longbridge_error(
        self, fake_settings: FakeSettings
    ):
        client = _make_client(fake_settings, ttl=60.0)

        def boom(*args, **kwargs):
            raise RuntimeError("transport down")

        client._ctx.history_candlesticks_by_offset.side_effect = boom

        with pytest.raises(LongbridgeError, match="transport down"):
            await client._fetch_today_bar_uncached("AAPL")

    async def test_timeout_is_wrapped_in_longbridge_error(
        self, fake_settings: FakeSettings, monkeypatch: pytest.MonkeyPatch
    ):
        # Force wait_for to raise TimeoutError; SDK side is short-circuited.
        client = _make_client(fake_settings, ttl=60.0)

        async def fake_wait_for(awaitable, timeout):  # noqa: ARG001
            try:
                await awaitable
            except Exception:
                pass
            raise asyncio.TimeoutError()

        monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)

        with pytest.raises(LongbridgeError, match="timed out"):
            await client._fetch_today_bar_uncached("AAPL")


class TestUncachedRequestShape:
    async def test_uses_us_region_suffix_by_default(
        self, fake_settings: FakeSettings
    ):
        client = _make_client(fake_settings)
        candle = MagicMock(open=1, high=2, low=0.5, close=1.5, volume=10, turnover=15)
        candle.timestamp = datetime(2026, 8, 21, tzinfo=timezone.utc)

        client._ctx.history_candlesticks_by_offset.return_value = [candle]

        await client._fetch_today_bar_uncached("AAPL")

        call = client._ctx.history_candlesticks_by_offset.call_args
        assert call.args[0] == "AAPL.US"

    async def test_already_suffixed_symbol_passes_through(
        self, fake_settings: FakeSettings
    ):
        client = _make_client(fake_settings)
        candle = MagicMock(open=1, high=2, low=0.5, close=1.5, volume=10, turnover=15)
        candle.timestamp = datetime(2026, 8, 21, tzinfo=timezone.utc)

        client._ctx.history_candlesticks_by_offset.return_value = [candle]

        await client._fetch_today_bar_uncached("700.HK")

        call = client._ctx.history_candlesticks_by_offset.call_args
        assert call.args[0] == "700.HK"

    async def test_no_candles_returns_none(self, fake_settings: FakeSettings):
        client = _make_client(fake_settings)
        client._ctx.history_candlesticks_by_offset.return_value = []
        assert await client._fetch_today_bar_uncached("AAPL") is None

    async def test_live_bar_drops_turnover_from_vwap_field(
        self, fake_settings: FakeSettings
    ):
        """Longbridge's turnover is total notional, not per-share VWAP.

        The bar dict must surface volume as shares and leave vwap/trade_count
        as None so the cache-shape normalisation doesn't leak turnover into
        the vwap field.
        """
        from datetime import datetime, timezone

        client = _make_client(fake_settings)
        candle = MagicMock(
            open=1.0,
            high=2.0,
            low=0.5,
            close=1.5,
            volume=18_306_516,
            turnover=5_954_824_475.316,
        )
        candle.timestamp = datetime(2026, 9, 2, tzinfo=timezone.utc)

        client._ctx.history_candlesticks_by_offset.return_value = [candle]

        result = await client._fetch_today_bar_uncached("AAPL")

        assert result is not None
        assert result["v"] == 18_306_516.0
        assert result["vw"] is None
        assert result["n"] is None


class TestTodayNycTimezone:
    """Cache must key off the NY calendar date."""

    def test_today_ny_matches_now_ny_date(self) -> None:
        assert LongbridgeClient._today_ny() == Settings.now_ny_date()

    def test_today_ny_is_not_utc_when_ny_and_utc_disagree(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When ET and UTC fall on different calendar dates, ET wins."""
        fake_ny_now = datetime(2026, 8, 21, 23, 30, tzinfo=NY_TZ)

        class _FakeDatetime:
            @staticmethod
            def now(tz=None):
                if tz is None or tz == NY_TZ:
                    return fake_ny_now
                return fake_ny_now.astimezone(tz)

        monkeypatch.setattr("app.config.settings.datetime", _FakeDatetime)
        assert ny_now() == fake_ny_now
        assert LongbridgeClient._today_ny() == fake_ny_now.date()