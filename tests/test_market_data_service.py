"""Tests for MarketDataService orchestration against a real
MarketBarsStore backed by ``tmp_path``."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.market_data_service import MarketDataService
from app.services.market_bars_store import MarketBarsStore
from tests.conftest import FakeSettings


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


def _ts_for(date_: date) -> int:
    return int(
        datetime(date_.year, date_.month, date_.day, tzinfo=timezone.utc).timestamp()
        * 1000
    )


@pytest.fixture
def store(tmp_path: Path) -> MarketBarsStore:
    return MarketBarsStore(tmp_path / "market")


@pytest.fixture
def service(
    fake_settings: FakeSettings, store: MarketBarsStore
) -> MarketDataService:
    polygon = MagicMock()
    ibkr = MagicMock()
    polygon.fetch_daily_bars = AsyncMock(return_value=[])
    ibkr.fetch_today_bar = AsyncMock(return_value=None)
    return MarketDataService(
        fake_settings, store=store, polygon=polygon, ibkr=ibkr
    )


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
        result = await service.get_bars("aapl", today, today)
        assert result["ticker"] == "AAPL"
        # No historical range because start == today.
        service._polygon.fetch_daily_bars.assert_not_called()


class TestHistoricalOnly:
    async def test_no_polygon_call_when_cache_is_full(
        self, service: MarketDataService, store: MarketBarsStore, today: date
    ):
        # Small window, cache covers every day in it.
        end = today - timedelta(days=1)
        start = end - timedelta(days=4)

        for d in range(1, 6):
            d_ = end - timedelta(days=d - 1)
            store.write_bars("AAPL", [_polygon_bar(_ts_for(d_))])

        result = await service.get_bars("AAPL", start, end)
        service._polygon.fetch_daily_bars.assert_not_called()
        assert all(b["source"] == "cache" for b in result["bars"])

    async def test_backfills_from_polygon_for_missing_dates(
        self, service: MarketDataService, store: MarketBarsStore, today: date
    ):
        end = today - timedelta(days=1)
        start = end - timedelta(days=5)

        # Cache is empty.
        service._polygon.fetch_daily_bars = AsyncMock(
            return_value=[
                _polygon_bar(_ts_for(end - timedelta(days=i))) for i in range(6)
            ]
        )

        result = await service.get_bars("AAPL", start, end)

        service._polygon.fetch_daily_bars.assert_called_once()
        assert result["backfilled_bars"] == 6
        # After backfill, the cache should contain those bars.
        cached = store.read_range("AAPL", start, end)
        assert len(cached) == 6

    async def test_backfill_window_uses_min_max_of_missing_dates(
        self, service: MarketDataService, today: date
    ):
        """When only a few dates are missing in the middle of the window,
        Polygon should only be asked for the smallest covering range."""
        end = today - timedelta(days=1)
        start = end - timedelta(days=9)

        # Pre-seed the cache so that days 0,1,2,3,6,7,8,9 are cached.
        # Days 4,5 are missing.
        service._polygon.fetch_daily_bars = AsyncMock(return_value=[])
        service._store.write_bars("AAPL", [
            _polygon_bar(_ts_for(end - timedelta(days=d))) for d in [0, 1, 2, 3, 6, 7, 8, 9]
        ])

        # Reset the mock so it doesn't capture the write-back above.
        service._polygon.fetch_daily_bars.reset_mock()

        await service.get_bars("AAPL", start, end)

        # The Polygon window should be just [start+4, start+5] — the
        # two missing days — not the full 10-day window.
        args = service._polygon.fetch_daily_bars.call_args.args
        assert args[0] == "AAPL"
        assert args[1] == start + timedelta(days=4)
        assert args[2] == start + timedelta(days=5)


class TestToday:
    async def test_ibkr_called_when_end_includes_today(
        self, service: MarketDataService, today: date
    ):
        service._ibkr.fetch_today_bar = AsyncMock(
            return_value=_ibkr_bar(_ts_for(today))
        )
        result = await service.get_bars("AAPL", today, today)
        service._ibkr.fetch_today_bar.assert_called_once_with("AAPL")
        ibkr_bars = [b for b in result["bars"] if b["source"] == "ibkr"]
        assert len(ibkr_bars) == 1

    async def test_no_ibkr_call_when_end_is_yesterday(
        self, service: MarketDataService, store: MarketBarsStore, today: date
    ):
        end = today - timedelta(days=1)
        store.write_bars("AAPL", [_polygon_bar(_ts_for(end))])
        result = await service.get_bars("AAPL", end, end)
        service._ibkr.fetch_today_bar.assert_not_called()
        assert all(b["source"] == "cache" for b in result["bars"])

    async def test_ibkr_none_result_is_silently_skipped(
        self, service: MarketDataService, today: date
    ):
        service._ibkr.fetch_today_bar = AsyncMock(return_value=None)
        result = await service.get_bars("AAPL", today, today)
        assert result["bars"] == []

    async def test_bars_are_sorted_by_timestamp(
        self, service: MarketDataService, store: MarketBarsStore, today: date
    ):
        end = today - timedelta(days=2)
        # Cache has the older bar; new bar comes from IBKR.
        store.write_bars(
            "AAPL",
            [_polygon_bar(_ts_for(end)), _polygon_bar(_ts_for(end - timedelta(days=1)))],
        )
        service._ibkr.fetch_today_bar = AsyncMock(
            return_value=_ibkr_bar(_ts_for(today))
        )

        result = await service.get_bars("AAPL", end - timedelta(days=1), today)
        timestamps = [b["timestamp"] for b in result["bars"]]
        assert timestamps == sorted(timestamps)


class TestErrorMapping:
    async def test_polygon_error_propagates(
        self, service: MarketDataService, today: date
    ):
        from app.clients.polygon import PolygonError

        service._polygon.fetch_daily_bars = AsyncMock(
            side_effect=PolygonError("polygon boom")
        )
        with pytest.raises(PolygonError, match="polygon boom"):
            await service.get_bars(
                "AAPL",
                today - timedelta(days=3),
                today - timedelta(days=1),
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


class TestBackfillYesterday:
    async def test_writes_yesterdays_bar_only(
        self, service: MarketDataService, store: MarketBarsStore
    ):
        from app.clients.polygon import PolygonError

        yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
        service._polygon.fetch_daily_bars = AsyncMock(
            return_value=[_polygon_bar(_ts_for(yesterday))]
        )

        count = await service.backfill_yesterday("AAPL")
        assert count == 1

        cached = store.read_range("AAPL", yesterday, yesterday)
        assert len(cached) == 1

    async def test_returns_zero_when_polygon_returns_no_bar(
        self, service: MarketDataService
    ):
        service._polygon.fetch_daily_bars = AsyncMock(return_value=[])
        assert await service.backfill_yesterday("AAPL") == 0

    async def test_returns_zero_on_polygon_error(
        self, service: MarketDataService
    ):
        from app.clients.polygon import PolygonError

        service._polygon.fetch_daily_bars = AsyncMock(
            side_effect=PolygonError("down")
        )
        assert await service.backfill_yesterday("AAPL") == 0