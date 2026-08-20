"""Tests for the IBKRClient TTL cache + single-flight behaviour."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from app.clients.ibkr import IBKRError, IBKRClient, _BarCollector
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


def _make_client(settings: FakeSettings, *, ttl: float = 60.0) -> IBKRClient:
    return IBKRClient(settings, cache_ttl_seconds=ttl)


class TestUncachedPath:
    """Directly exercise the no-cache code path."""

    async def test_returns_last_bar(self, fake_settings: FakeSettings):
        client = _make_client(fake_settings)

        async def fake_run(self: IBKRClient, ticker: str) -> dict[str, Any]:
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

        async def fake_run(self: IBKRClient, ticker: str) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            return _bar(timestamp=call_count)

        client._fetch_today_bar_uncached = fake_run.__get__(client)  # type: ignore[method-assign]

        r1 = await client.fetch_today_bar("AAPL")
        r2 = await client.fetch_today_bar("AAPL")

        assert r1 == r2
        assert call_count == 1

    async def test_cache_expires_after_ttl(self, fake_settings: FakeSettings):
        # Use a fake clock so the test runs in milliseconds, not 5 minutes.
        client = _make_client(fake_settings, ttl=0.05)

        call_count = 0

        async def fake_run(self: IBKRClient, ticker: str) -> dict[str, Any]:
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

        async def fake_run(self: IBKRClient, ticker: str) -> dict[str, Any]:
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
        """If the underlying call returned None (no bar yet today), that
        ``None`` is still cached so we don't hammer IBKR."""
        client = _make_client(fake_settings, ttl=60.0)
        call_count = 0

        async def fake_run(self: IBKRClient, ticker: str) -> Any:
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

        async def fake_run(self: IBKRClient, ticker: str) -> dict[str, Any]:
            nonlocal active, max_active, call_count
            active += 1
            max_active = max(max_active, active)
            try:
                # Yield enough that all coroutines have time to enter
                # the single-flight branch.
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


class TestIBKRErrorHandling:
    @pytest.mark.parametrize(
        ("code", "message"),
        [
            (2104, "Market data farm connection is OK:hfarm"),
            (2106, "HMDS data farm connection is OK:apachmds"),
            (2107, "Historical data farm connection has become inactive"),
            (2108, "Market data farm connection has become inactive"),
            (2158, "Sec-def data farm connection is OK"),
        ],
    )
    def test_informational_status_is_not_fatal(self, code, message):
        loop = asyncio.new_event_loop()
        future = loop.create_future()
        collector = _BarCollector(loop, future)

        try:
            collector.error(1, code, message)
            assert not future.done()
        finally:
            loop.close()

    def test_other_market_data_error_is_fatal(self):
        loop = asyncio.new_event_loop()
        future = loop.create_future()
        collector = _BarCollector(loop, future)

        try:
            collector.error(1, 2105, "Historical data farm connection is broken")
            with pytest.raises(IBKRError, match="IBKR error 2105"):
                loop.run_until_complete(future)
        finally:
            loop.close()


class TestErrorPropagation:
    async def test_error_is_raised_to_all_waiters(self, fake_settings: FakeSettings):
        client = _make_client(fake_settings, ttl=60.0)

        async def fake_run(self: IBKRClient, ticker: str) -> dict[str, Any]:
            await asyncio.sleep(0.01)
            raise IBKRError("boom")

        client._fetch_today_bar_uncached = fake_run.__get__(client)  # type: ignore[method-assign]

        with pytest.raises(IBKRError, match="boom"):
            await client.fetch_today_bar("AAPL")

        # After a failed call, the inflight slot must be cleared so the
        # next call can retry.
        assert client._inflight == {}
        assert client._cache == {}