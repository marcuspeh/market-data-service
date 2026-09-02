"""Tests for MarketDataService orchestration against a real MarketBarsStore."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.clients.longbridge import LongbridgeError
from app.clients.polygon import PolygonError
from app.config.settings import NY_TZ, Settings
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


def _longbridge_bar(timestamp: int) -> dict[str, Any]:
    return _polygon_bar(timestamp)


def _ts_for_ny(date_: date) -> int:
    """Epoch-ms whose NY date is ``date_`` (23:30 ET keeps CI TZ honest)."""
    return int(
        datetime(date_.year, date_.month, date_.day, 23, 30, tzinfo=NY_TZ)
        .astimezone(timezone.utc)
        .timestamp()
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
    longbridge = MagicMock()
    polygon.fetch_daily_bars = AsyncMock(return_value=[])
    longbridge.fetch_today_bar = AsyncMock(return_value=None)
    return MarketDataService(
        fake_settings, store=store, polygon=polygon, longbridge=longbridge
    )


@pytest.fixture
def today() -> date:
    return Settings.now_ny_date()


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
        end = today - timedelta(days=1)
        start = end - timedelta(days=4)

        for d in range(1, 6):
            d_ = end - timedelta(days=d - 1)
            store.write_bars("AAPL", [_polygon_bar(_ts_for_ny(d_))])

        result = await service.get_bars("AAPL", start, end)
        service._polygon.fetch_daily_bars.assert_not_called()
        assert all(b["source"] == "cache" for b in result["bars"])

    async def test_backfills_from_polygon_for_missing_dates(
        self, service: MarketDataService, store: MarketBarsStore, today: date
    ):
        end = today - timedelta(days=1)
        start = end - timedelta(days=5)

        service._polygon.fetch_daily_bars = AsyncMock(
            return_value=[
                _polygon_bar(_ts_for_ny(end - timedelta(days=i))) for i in range(6)
            ]
        )

        result = await service.get_bars("AAPL", start, end)

        service._polygon.fetch_daily_bars.assert_called_once()
        assert result["backfilled_bars"] == 6
        cached = store.read_range("AAPL", start, end)
        assert len(cached) == 6

    async def test_backfill_window_uses_min_max_of_missing_dates(
        self, service: MarketDataService, today: date
    ):
        """Only ask Polygon for the smallest covering range of missing dates."""
        end = today - timedelta(days=1)
        start = end - timedelta(days=9)

        # Days 0,1,2,3,6,7,8,9 cached; days 4,5 missing.
        service._polygon.fetch_daily_bars = AsyncMock(return_value=[])
        service._store.write_bars("AAPL", [
            _polygon_bar(_ts_for_ny(end - timedelta(days=d))) for d in [0, 1, 2, 3, 6, 7, 8, 9]
        ])
        service._polygon.fetch_daily_bars.reset_mock()

        await service.get_bars("AAPL", start, end)

        args = service._polygon.fetch_daily_bars.call_args.args
        assert args[0] == "AAPL"
        assert args[1] == start + timedelta(days=4)
        assert args[2] == start + timedelta(days=5)


class TestToday:
    async def test_longbridge_called_when_end_includes_today(
        self, service: MarketDataService, today: date
    ):
        service._longbridge.fetch_today_bar = AsyncMock(
            return_value=_longbridge_bar(_ts_for_ny(today))
        )
        result = await service.get_bars("AAPL", today, today)
        service._longbridge.fetch_today_bar.assert_called_once_with("AAPL")
        longbridge_bars = [b for b in result["bars"] if b["source"] == "longbridge"]
        assert len(longbridge_bars) == 1

    async def test_longbridge_bar_is_never_persisted_to_parquet(
        self, service: MarketDataService, store: MarketBarsStore, today: date
    ):
        """Today's Longbridge bar stays in the response only — never in parquet."""
        service._longbridge.fetch_today_bar = AsyncMock(
            return_value=_longbridge_bar(_ts_for_ny(today))
        )

        result = await service.get_bars("AAPL", today, today)

        assert any(b["source"] == "longbridge" for b in result["bars"])
        cached = store.read_range("AAPL", today, today)
        assert cached == []
        assert list(store.list_years("AAPL")) == []

    async def test_no_longbridge_call_when_end_is_yesterday(
        self, service: MarketDataService, store: MarketBarsStore, today: date
    ):
        end = today - timedelta(days=1)
        store.write_bars("AAPL", [_polygon_bar(_ts_for_ny(end))])
        result = await service.get_bars("AAPL", end, end)
        service._longbridge.fetch_today_bar.assert_not_called()
        assert all(b["source"] == "cache" for b in result["bars"])

    async def test_longbridge_none_result_is_silently_skipped(
        self, service: MarketDataService, today: date
    ):
        service._longbridge.fetch_today_bar = AsyncMock(return_value=None)
        result = await service.get_bars("AAPL", today, today)
        assert result["bars"] == []

    async def test_bars_are_sorted_by_timestamp(
        self, service: MarketDataService, store: MarketBarsStore, today: date
    ):
        end = today - timedelta(days=2)
        store.write_bars(
            "AAPL",
            [_polygon_bar(_ts_for_ny(end)), _polygon_bar(_ts_for_ny(end - timedelta(days=1)))],
        )
        service._longbridge.fetch_today_bar = AsyncMock(
            return_value=_longbridge_bar(_ts_for_ny(today))
        )

        result = await service.get_bars("AAPL", end - timedelta(days=1), today)
        timestamps = [b["timestamp"] for b in result["bars"]]
        assert timestamps == sorted(timestamps)

    async def test_cached_today_row_is_filtered_out(
        self, service: MarketDataService, store: MarketBarsStore, today: date
    ):
        """A stale intraday row in parquet for today must be dropped."""
        store.write_bars("AAPL", [_polygon_bar(_ts_for_ny(today))])

        service._longbridge.fetch_today_bar = AsyncMock(
            return_value=_longbridge_bar(_ts_for_ny(today))
        )

        result = await service.get_bars("AAPL", today, today)

        assert len(result["bars"]) == 1
        assert result["bars"][0]["source"] == "longbridge"


class TestErrorMapping:
    async def test_polygon_error_propagates(
        self, service: MarketDataService, today: date
    ):
        service._polygon.fetch_daily_bars = AsyncMock(
            side_effect=PolygonError("polygon boom")
        )
        with pytest.raises(PolygonError, match="polygon boom"):
            await service.get_bars(
                "AAPL",
                today - timedelta(days=3),
                today - timedelta(days=1),
            )

    async def test_longbridge_error_propagates(
        self, service: MarketDataService, today: date
    ):
        service._longbridge.fetch_today_bar = AsyncMock(
            side_effect=LongbridgeError("longbridge boom")
        )
        with pytest.raises(LongbridgeError, match="longbridge boom"):
            await service.get_bars("AAPL", today, today)


class TestPolygonBackfillGuards:
    async def test_today_bar_from_polygon_is_not_persisted(
        self, service: MarketDataService, store: MarketBarsStore, today: date
    ):
        """Polygon can return today's intraday bar — filter before persisting."""
        end = today - timedelta(days=1)
        start = end - timedelta(days=4)

        polygon_bars = [
            _polygon_bar(_ts_for_ny(start + timedelta(days=i))) for i in range(5)
        ] + [_polygon_bar(_ts_for_ny(today))]

        service._polygon.fetch_daily_bars = AsyncMock(return_value=polygon_bars)
        service._longbridge.fetch_today_bar = AsyncMock(
            return_value=_longbridge_bar(_ts_for_ny(today))
        )

        result = await service.get_bars("AAPL", start, today)

        assert result["backfilled_bars"] == 5
        cached = store.read_range("AAPL", start, today)
        assert len(cached) == 5
        cached_dates = {row["date"] for row in cached}
        assert today not in cached_dates
        assert any(b["source"] == "longbridge" for b in result["bars"])


class TestBackfillYesterday:
    async def test_writes_yesterdays_bar_only(
        self, service: MarketDataService, store: MarketBarsStore, today: date
    ):
        yesterday = today - timedelta(days=1)
        service._polygon.fetch_daily_bars = AsyncMock(
            return_value=[_polygon_bar(_ts_for_ny(yesterday))]
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