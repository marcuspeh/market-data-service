"""Tests for MarketDataService orchestration."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.market_data_service import MarketDataService
from tests.conftest import FakeSettings


def _bar(timestamp: int, weight: float = 100.0) -> dict[str, Any]:
    """Shape that matches what the repository's .values() would return."""
    bar_date = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).date()
    return {
        "ticker": "AAPL",
        "timestamp": timestamp,
        "open": weight,
        "high": weight + 1,
        "low": weight - 1,
        "close": weight,
        "volume": 1000.0,
        "vwap": weight,
        "trade_count": 10,
        "bar_date": bar_date,
    }


def _polygon_bar(timestamp: int) -> dict[str, Any]:
    """Raw Polygon.io bar shape (uses 't', 'o', etc.)."""
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


def _ibkr_bar(timestamp: int) -> dict[str, Any]:
    return _polygon_bar(timestamp)


@pytest.fixture
def service(fake_settings: FakeSettings) -> MarketDataService:
    repo = MagicMock()
    polygon = MagicMock()
    ibkr = MagicMock()
    repo.list_in_range = AsyncMock(return_value=[])
    repo.save_many = AsyncMock()
    polygon.fetch_daily_bars = AsyncMock(return_value=[])
    ibkr.fetch_today_bar = AsyncMock(return_value=None)
    return MarketDataService(fake_settings, repo=repo, polygon=polygon, ibkr=ibkr)


@pytest.fixture
def today() -> date:
    return datetime.now(timezone.utc).date()


class TestValidation:
    async def test_to_before_from_raises(self, service: MarketDataService):
        with pytest.raises(ValueError, match="after 'from'"):
            await service.get_bars("AAPL", date(2026, 1, 5), date(2026, 1, 1))

    async def test_ticker_is_uppercased(
        self, service: MarketDataService, today: date
    ):
        service._repo.list_in_range = AsyncMock(return_value=[])
        service._ibkr.fetch_today_bar = AsyncMock(return_value=None)
        result = await service.get_bars("aapl", today, today)
        assert result["ticker"] == "AAPL"
        # No historical range because start == today.
        service._repo.list_in_range.assert_not_called()


class TestHistoricalOnly:
    async def test_no_polygon_call_when_cache_is_full(
        self, service: MarketDataService, today: date
    ):
        # Small window, cache covers every day in it.
        end = today.replace() - __import__("datetime").timedelta(days=1)
        start = end.replace() - __import__("datetime").timedelta(days=4)

        timestamps = [
            int(datetime(today.year, today.month, today.day, tzinfo=timezone.utc).timestamp() * 1000)
            - i * 86_400_000
            for i in range(1, 6)
        ]
        cached = [_bar(ts) for ts in timestamps]
        service._repo.list_in_range = AsyncMock(return_value=cached)

        result = await service.get_bars("AAPL", start, end)

        service._polygon.fetch_daily_bars.assert_not_called()
        service._repo.save_many.assert_not_called()
        assert all(b["source"] == "cache" for b in result["bars"])

    async def test_backfills_from_polygon_for_missing_dates(
        self, service: MarketDataService, today: date
    ):
        end = today.replace() - __import__("datetime").timedelta(days=1)
        start = end.replace() - __import__("datetime").timedelta(days=5)

        # Cache is empty → all 6 dates missing.
        service._repo.list_in_range = AsyncMock(return_value=[])
        service._polygon.fetch_daily_bars = AsyncMock(
            return_value=[_polygon_bar(1700000000000 + i * 86_400_000) for i in range(6)]
        )

        result = await service.get_bars("AAPL", start, end)

        service._polygon.fetch_daily_bars.assert_called_once()
        service._repo.save_many.assert_called_once()
        assert result["backfilled_bars"] == 6

    async def test_backfill_window_uses_min_max_of_missing_dates(
        self, service: MarketDataService, today: date
    ):
        """When only a few dates are missing in the middle of the window,
        Polygon should only be asked for the smallest covering range."""
        from datetime import timedelta

        # 10-day historical window ending yesterday.
        end = today - timedelta(days=1)
        start = end - timedelta(days=9)

        # Cache covers every day EXCEPT days 4 and 5 (the middle of the
        # window). We compute the cached timestamps so they line up with
        # specific dates in the window.
        today_ts = int(
            datetime(today.year, today.month, today.day, tzinfo=timezone.utc).timestamp() * 1000
        )
        end_ts = today_ts - 86_400_000  # end = yesterday
        ts_for = lambda d: end_ts - d * 86_400_000  # d=0 → end, d=1 → day before end, ...

        cached_timestamps = [
            ts_for(d) for d in [0, 1, 2, 3, 6, 7, 8, 9]  # skip 4 and 5
        ]
        cached = [_bar(ts) for ts in cached_timestamps]

        service._repo.list_in_range = AsyncMock(side_effect=[cached, cached])
        service._polygon.fetch_daily_bars = AsyncMock(return_value=[])

        await service.get_bars("AAPL", start, end)

        # The Polygon window should be just [start+4, start+5] — the
        # two missing days — not the full 10-day window.
        # (fetch_daily_bars is called positionally.)
        args = service._polygon.fetch_daily_bars.call_args.args
        assert args[0] == "AAPL"      # ticker
        assert args[1] == start + timedelta(days=4)  # backfill_start
        assert args[2] == start + timedelta(days=5)  # backfill_end


class TestToday:
    async def test_ibkr_called_when_end_includes_today(
        self, service: MarketDataService, today: date
    ):
        service._ibkr.fetch_today_bar = AsyncMock(
            return_value=_ibkr_bar(1763000000000)
        )
        result = await service.get_bars("AAPL", today, today)

        service._ibkr.fetch_today_bar.assert_called_once_with("AAPL")
        ibkr_bars = [b for b in result["bars"] if b["source"] == "ibkr"]
        assert len(ibkr_bars) == 1

    async def test_no_ibkr_call_when_end_is_yesterday(
        self, service: MarketDataService, today: date
    ):
        end = today.replace() - __import__("datetime").timedelta(days=1)
        start = end
        service._repo.list_in_range = AsyncMock(
            return_value=[_bar(1700000000000)]
        )

        result = await service.get_bars("AAPL", start, end)

        service._ibkr.fetch_today_bar.assert_not_called()
        assert all(b["source"] == "cache" for b in result["bars"])

    async def test_ibkr_none_result_is_silently_skipped(
        self, service: MarketDataService, today: date
    ):
        """If IBKR returns no bar yet today, we don't blow up."""
        service._ibkr.fetch_today_bar = AsyncMock(return_value=None)
        result = await service.get_bars("AAPL", today, today)
        assert result["bars"] == []

    async def test_bars_are_sorted_by_timestamp(
        self, service: MarketDataService, today: date
    ):
        end = today.replace() - __import__("datetime").timedelta(days=2)
        start = end

        # Return cached bars out of order to make sure the service sorts.
        ts1, ts2 = 1700000000000, 1700086400000
        service._repo.list_in_range = AsyncMock(
            return_value=[_bar(ts2), _bar(ts1)]
        )
        service._ibkr.fetch_today_bar = AsyncMock(
            return_value=_ibkr_bar(1763000000000)
        )

        result = await service.get_bars("AAPL", start, today)
        timestamps = [b["timestamp"] for b in result["bars"]]
        assert timestamps == sorted(timestamps)


class TestErrorMapping:
    async def test_polygon_error_propagates(
        self, service: MarketDataService, today: date
    ):
        from app.clients.polygon import PolygonError

        service._repo.list_in_range = AsyncMock(return_value=[])
        service._polygon.fetch_daily_bars = AsyncMock(
            side_effect=PolygonError("polygon boom")
        )
        with pytest.raises(PolygonError, match="polygon boom"):
            await service.get_bars(
                "AAPL",
                today.replace() - __import__("datetime").timedelta(days=3),
                today.replace() - __import__("datetime").timedelta(days=1),
            )

    async def test_ibkr_error_propagates(
        self, service: MarketDataService, today: date
    ):
        from app.clients.ibkr import IBKRError

        service._ibkr.fetch_today_bar = AsyncMock(
            side_effect=IBKRError("ibkr boom")
        )
        with pytest.raises(IBKRError, match="ibkr boom"):
            await service.get_bars("AAPL", today, today)