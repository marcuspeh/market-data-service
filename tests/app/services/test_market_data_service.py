"""Tests for MarketDataService orchestration against real parquet stores."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.clients.longbridge import LongbridgeError
from app.clients.polygon import PolygonError
from app.config.settings import NY_TZ, Settings
from app.services.corporate_actions_store import CorporateActionsStore
from app.services.market_bars_store import MarketBarsStore
from app.services.market_data_service import LOOKBACK, MarketDataService
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


def _bar(d: date, close: float = 100.0, volume: float = 1000.0) -> dict[str, Any]:
    """A Polygon-shaped bar dated ``d`` with a chosen close/volume."""
    bar = _polygon_bar(_ts_for_ny(d))
    bar["c"] = close
    bar["v"] = volume
    return bar


def _ts_for_ny(date_: date) -> int:
    """Epoch-ms whose NY date is ``date_`` (23:30 ET keeps CI TZ honest)."""
    return int(
        datetime(date_.year, date_.month, date_.day, 23, 30, tzinfo=NY_TZ)
        .astimezone(timezone.utc)
        .timestamp()
        * 1000
    )


def _split(d: date, split_from: float, split_to: float) -> dict[str, Any]:
    return {
        "date": d,
        "kind": "split",
        "split_from": split_from,
        "split_to": split_to,
        "cash_amount": None,
    }


def _dividend(d: date, cash: float) -> dict[str, Any]:
    return {
        "date": d,
        "kind": "dividend",
        "split_from": None,
        "split_to": None,
        "cash_amount": cash,
    }


@pytest.fixture
def store(tmp_path: Path) -> MarketBarsStore:
    return MarketBarsStore(tmp_path / "market")


@pytest.fixture
def actions_store(tmp_path: Path) -> CorporateActionsStore:
    return CorporateActionsStore(tmp_path / "corporate_actions")


@pytest.fixture
def service(
    fake_settings: FakeSettings,
    store: MarketBarsStore,
    actions_store: CorporateActionsStore,
) -> MarketDataService:
    polygon = MagicMock()
    longbridge = MagicMock()
    polygon.fetch_daily_bars = AsyncMock(return_value=[])
    polygon.fetch_splits = AsyncMock(return_value=[])
    polygon.fetch_dividends = AsyncMock(return_value=[])
    longbridge.fetch_today_bar = AsyncMock(return_value=None)
    return MarketDataService(
        fake_settings,
        store=store,
        actions_store=actions_store,
        polygon=polygon,
        longbridge=longbridge,
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
    async def test_no_bar_backfill_when_cache_is_full(
        self, service: MarketDataService, store: MarketBarsStore, today: date
    ):
        end = today - timedelta(days=1)
        start = end - timedelta(days=4)

        # The cache must cover the request *and* the dividend lookback.
        cache_start = start - LOOKBACK
        store.write_bars(
            "AAPL",
            [
                _bar(cache_start + timedelta(days=i))
                for i in range((end - cache_start).days + 1)
            ],
        )

        result = await service.get_bars("AAPL", start, end)
        service._polygon.fetch_daily_bars.assert_not_called()
        assert all(b["source"] == "cache" for b in result["bars"])
        assert result["backfilled_bars"] == 0

    async def test_actions_are_refreshed_even_when_the_cache_is_warm(
        self, service: MarketDataService, store: MarketBarsStore, today: date
    ):
        """A split can be declared at any time, so a warm cache still syncs."""
        end = today - timedelta(days=1)
        start = end - timedelta(days=4)
        cache_start = start - LOOKBACK
        store.write_bars(
            "AAPL",
            [
                _bar(cache_start + timedelta(days=i))
                for i in range((end - cache_start).days + 1)
            ],
        )
        service._polygon.fetch_splits = AsyncMock(
            return_value=[
                {
                    "execution_date": (start + timedelta(days=1)).isoformat(),
                    "split_from": 1.0,
                    "split_to": 2.0,
                }
            ]
        )

        await service.get_bars("AAPL", start, end)

        service._polygon.fetch_splits.assert_called_once()
        stored = service._actions.read("AAPL", start, end)
        assert len(stored) == 1
        assert stored[0]["kind"] == "split"

    async def test_action_sync_is_throttled_within_the_ttl(
        self, service: MarketDataService, store: MarketBarsStore, today: date
    ):
        """A burst of reads must not become a burst of upstream calls."""
        end = today - timedelta(days=1)
        start = end - timedelta(days=4)
        cache_start = start - LOOKBACK
        store.write_bars(
            "AAPL",
            [
                _bar(cache_start + timedelta(days=i))
                for i in range((end - cache_start).days + 1)
            ],
        )

        await service.get_bars("AAPL", start, end)
        await service.get_bars("AAPL", start, end)

        assert service._polygon.fetch_splits.call_count == 1

    async def test_action_sync_runs_for_a_wider_window(
        self, service: MarketDataService, store: MarketBarsStore, today: date
    ):
        """A wider window isn't covered by the last sync, so it must re-sync."""
        narrow_end = today - timedelta(days=10)
        narrow_start = narrow_end - timedelta(days=4)
        wide_end = today - timedelta(days=1)
        wide_start = wide_end - timedelta(days=20)

        cache_start = min(narrow_start, wide_start) - LOOKBACK
        store.write_bars(
            "AAPL",
            [
                _bar(cache_start + timedelta(days=i))
                for i in range((wide_end - cache_start).days + 1)
            ],
        )

        await service.get_bars("AAPL", narrow_start, narrow_end)
        await service.get_bars("AAPL", wide_start, wide_end)

        assert service._polygon.fetch_splits.call_count == 2

    async def test_backfills_from_polygon_for_missing_dates(
        self, service: MarketDataService, store: MarketBarsStore, today: date
    ):
        end = today - timedelta(days=1)
        start = end - timedelta(days=5)

        service._polygon.fetch_daily_bars = AsyncMock(
            return_value=[
                _bar(end - timedelta(days=i)) for i in range(6)
            ]
        )

        result = await service.get_bars("AAPL", start, end)

        service._polygon.fetch_daily_bars.assert_called_once()
        assert result["backfilled_bars"] == 6
        cached = store.read_range("AAPL", start, end)
        assert len(cached) == 6

    async def test_asks_polygon_for_the_dividend_lookback(
        self, service: MarketDataService, today: date
    ):
        """An empty cache triggers one fetch covering lookback + window."""
        end = today - timedelta(days=1)
        start = end - timedelta(days=4)

        await service.get_bars("AAPL", start, end)

        args = service._polygon.fetch_daily_bars.call_args.args
        assert args == ("AAPL", start - LOOKBACK, end)


class TestBackfillWindow:
    def test_none_extent_covers_request(self):
        start, end = date(2025, 1, 1), date(2025, 3, 1)
        assert MarketDataService._backfill_window(start, end, None) == (start, end)

    def test_fully_cached_window_needs_no_fetch(self):
        start, end = date(2025, 1, 1), date(2025, 3, 1)
        extent = (date(2024, 12, 1), date(2025, 4, 1))
        assert MarketDataService._backfill_window(start, end, extent) is None

    def test_extends_left_only(self):
        start, end = date(2025, 1, 1), date(2025, 3, 1)
        extent = (date(2025, 1, 15), date(2025, 4, 1))
        assert MarketDataService._backfill_window(start, end, extent) == (
            start,
            date(2025, 1, 14),
        )

    def test_extends_right_only(self):
        start, end = date(2025, 1, 1), date(2025, 3, 1)
        extent = (date(2024, 12, 1), date(2025, 2, 1))
        assert MarketDataService._backfill_window(start, end, extent) == (
            date(2025, 2, 2),
            end,
        )

    def test_extends_both_when_cache_is_interior(self):
        start, end = date(2025, 1, 1), date(2025, 3, 1)
        extent = (date(2025, 1, 20), date(2025, 2, 1))
        assert MarketDataService._backfill_window(start, end, extent) == (start, end)

    def test_interior_holes_are_not_refetched(self):
        """A gap inside the cached extent is a non-trading day, not a hole."""
        start, end = date(2025, 1, 1), date(2025, 3, 1)
        extent = (start, end)
        assert MarketDataService._backfill_window(start, end, extent) is None


class TestInteriorGaps:
    """A hole between two disjoint cached spans must not stay permanent."""

    async def test_disjoint_cached_spans_are_refetched(
        self, service: MarketDataService, store: MarketBarsStore, today: date
    ):
        end = today - timedelta(days=1)
        start = end - timedelta(days=30)
        cache_start = start - LOOKBACK

        # Two spans with a long hole between them.
        store.write_bars("AAPL", [_bar(cache_start), _bar(cache_start + timedelta(days=2))])
        store.write_bars("AAPL", [_bar(end - timedelta(days=2)), _bar(end)])

        service._polygon.fetch_daily_bars = AsyncMock(
            return_value=[
                _bar(cache_start + timedelta(days=i))
                for i in range((end - cache_start).days + 1)
            ]
        )

        result = await service.get_bars("AAPL", start, end)

        service._polygon.fetch_daily_bars.assert_called_once()
        assert result["backfilled_bars"] > 0
        # The hole is filled: bars now span the whole requested window.
        dates = [b["date"] for b in result["bars"]]
        assert len(dates) == (end - start).days + 1

    async def test_weekend_gap_is_not_treated_as_a_hole(
        self, service: MarketDataService, store: MarketBarsStore, today: date
    ):
        """A normal weekend is a closure, not missing data."""
        end = today - timedelta(days=1)
        start = end - timedelta(days=30)
        cache_start = start - LOOKBACK

        # Consecutive trading days only (weekends skipped).
        trading = []
        trading_dates = set()
        d = cache_start
        while d <= end:
            if d.weekday() < 5:
                trading.append(_bar(d))
                trading_dates.add(d)
            d += timedelta(days=1)
        store.write_bars("AAPL", trading)

        service._polygon.fetch_daily_bars = AsyncMock(return_value=[])

        result = await service.get_bars("AAPL", start, end)

        # A weekend gap must never trigger a whole-range refetch — that
        # is the signature of the interior-hole path. The edge logic may
        # still ask for the non-trading days *before* the first cached
        # bar (when the lookback start lands on a weekend), so the
        # assertion is that no fetch spans the covered range.
        for call in service._polygon.fetch_daily_bars.call_args_list:
            fetched_start, fetched_end = call.args[1:]
            assert fetched_end < end, (
                "a weekend gap must not trigger a whole-range refetch"
            )

        # No trading day was lost.
        returned = {b["date"] for b in result["bars"]}
        assert {d for d in trading_dates if start <= d <= end} <= returned

    async def test_hole_is_refetched_even_when_the_request_extends_past_the_cache(
        self, service: MarketDataService, store: MarketBarsStore, today: date
    ):
        """The gap check must not be skipped by the edge-extension logic.

        When the request reaches past either end of the cache, the
        backfill window is never ``None`` — so the hole would survive
        the refetch of the uncovered edges.
        """
        end = today - timedelta(days=1)
        start = end - timedelta(days=40)

        # Two spans with a hole between them, and a request that starts
        # before the cache and ends after it.
        store.write_bars("AAPL", [_bar(start), _bar(start + timedelta(days=3))])
        store.write_bars("AAPL", [_bar(end - timedelta(days=3)), _bar(end)])

        service._polygon.fetch_daily_bars = AsyncMock(return_value=[])

        await service.get_bars("AAPL", start, end)

        service._polygon.fetch_daily_bars.assert_called_once()
        # The whole covered range is refetched, not just the edges.
        fetched_start, fetched_end = service._polygon.fetch_daily_bars.call_args[0][1:]
        assert fetched_start <= start - LOOKBACK
        assert fetched_end >= end

    async def test_hole_outside_the_requested_window_is_left_alone(
        self, service: MarketDataService, store: MarketBarsStore, today: date
    ):
        """A hole the request doesn't reach into doesn't trigger a refetch."""
        end = today - timedelta(days=1)
        start = end - timedelta(days=40)

        store.write_bars("AAPL", [_bar(start), _bar(start + timedelta(days=3))])
        store.write_bars("AAPL", [_bar(end - timedelta(days=3)), _bar(end)])

        service._polygon.fetch_daily_bars = AsyncMock(return_value=[])

        # Request only the first span — the hole is out of range.
        await service.get_bars("AAPL", start, start + timedelta(days=3))

        assert service._polygon.fetch_daily_bars.call_count <= 1


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

    async def test_no_duplicate_dates_in_the_response(
        self, service: MarketDataService, store: MarketBarsStore, today: date
    ):
        """Each trading day appears exactly once in the response.

        A cached bar and the live bar must never land on the same date.
        """
        end = today - timedelta(days=1)
        store.write_bars("AAPL", [_bar(end)])

        # Longbridge's last completed candle is still yesterday's.
        service._longbridge.fetch_today_bar = AsyncMock(
            return_value=_longbridge_bar(_ts_for_ny(end))
        )

        result = await service.get_bars("AAPL", end, today)

        dates = [b["date"] for b in result["bars"]]
        assert len(dates) == len(set(dates))
        assert today not in dates

    async def test_bars_are_sorted_by_timestamp(
        self, service: MarketDataService, store: MarketBarsStore, today: date
    ):
        end = today - timedelta(days=2)
        store.write_bars(
            "AAPL",
            [
                _polygon_bar(_ts_for_ny(end)),
                _polygon_bar(_ts_for_ny(end - timedelta(days=1))),
            ],
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


class TestAdjustmentsOnRead:
    async def test_split_scales_bars_before_execution_date(
        self,
        service: MarketDataService,
        store: MarketBarsStore,
        actions_store: CorporateActionsStore,
        today: date,
    ):
        end = today - timedelta(days=1)
        start = end - timedelta(days=4)
        store.write_bars(
            "AAPL", [_bar(start + timedelta(days=i), close=200.0) for i in range(5)]
        )
        actions_store.upsert("AAPL", [_split(start + timedelta(days=2), 1.0, 2.0)])

        result = await service.get_bars("AAPL", start, end)
        bars = {b["date"]: b for b in result["bars"]}

        # 2-for-1: everything before the execution date halves.
        assert bars[start]["close"] == pytest.approx(100.0)
        assert bars[start + timedelta(days=1)]["close"] == pytest.approx(100.0)
        assert bars[start]["volume"] == pytest.approx(2000.0)
        # The execution date itself is already in the new basis.
        assert bars[start + timedelta(days=2)]["close"] == pytest.approx(200.0)
        assert bars[start + timedelta(days=2)]["volume"] == pytest.approx(1000.0)

    async def test_dividend_scales_bars_before_ex_date(
        self,
        service: MarketDataService,
        store: MarketBarsStore,
        actions_store: CorporateActionsStore,
        today: date,
    ):
        end = today - timedelta(days=1)
        start = end - timedelta(days=4)
        store.write_bars(
            "AAPL", [_bar(start + timedelta(days=i), close=100.0) for i in range(5)]
        )
        actions_store.upsert(
            "AAPL", [_dividend(start + timedelta(days=2), 2.0)]
        )

        result = await service.get_bars("AAPL", start, end)
        bars = {b["date"]: b for b in result["bars"]}

        # Prior close 100, cash 2 → factor 0.98 on everything before the ex-date.
        assert bars[start]["close"] == pytest.approx(98.0)
        assert bars[start + timedelta(days=1)]["close"] == pytest.approx(98.0)
        assert bars[start + timedelta(days=2)]["close"] == pytest.approx(100.0)
        # Cash dividends never touch volume.
        assert bars[start]["volume"] == pytest.approx(1000.0)

    async def test_split_and_dividend_compose_end_to_end(
        self,
        service: MarketDataService,
        store: MarketBarsStore,
        actions_store: CorporateActionsStore,
        today: date,
    ):
        """A dividend sized off a split-adjusted close, end to end."""
        end = today - timedelta(days=1)
        start = end - timedelta(days=4)
        # Realistic split pricing: 200 before the split, 100 after.
        closes = [200.0, 200.0, 100.0, 100.0, 100.0]
        store.write_bars(
            "AAPL",
            [
                _bar(start + timedelta(days=i), close=closes[i])
                for i in range(5)
            ],
        )
        actions_store.upsert(
            "AAPL",
            [
                _split(start + timedelta(days=2), 1.0, 2.0),
                _dividend(start + timedelta(days=3), 2.0),
            ],
        )

        result = await service.get_bars("AAPL", start, end)
        bars = {b["date"]: b for b in result["bars"]}

        # The split rebases 200 → 100. The dividend is sized off the prior
        # close in that basis (100), so it takes 2% off every bar before
        # its own ex-date.
        assert bars[start]["close"] == pytest.approx(98.0)
        assert bars[start + timedelta(days=1)]["close"] == pytest.approx(98.0)
        assert bars[start + timedelta(days=2)]["close"] == pytest.approx(98.0)
        assert bars[start + timedelta(days=3)]["close"] == pytest.approx(100.0)
        # Volume only ever reflects the split.
        assert bars[start]["volume"] == pytest.approx(2000.0)
        assert bars[start + timedelta(days=2)]["volume"] == pytest.approx(1000.0)

    async def test_raw_prices_are_stored_unadjusted(
        self,
        service: MarketDataService,
        store: MarketBarsStore,
        today: date,
    ):
        """The cache keeps vendor prices; only the response is adjusted."""
        end = today - timedelta(days=1)
        start = end - timedelta(days=2)
        service._polygon.fetch_daily_bars = AsyncMock(
            return_value=[
                _bar(start + timedelta(days=i), close=200.0) for i in range(3)
            ]
        )
        service._polygon.fetch_splits = AsyncMock(
            return_value=[
                {
                    "execution_date": (start + timedelta(days=1)).isoformat(),
                    "split_from": 1.0,
                    "split_to": 2.0,
                }
            ]
        )

        result = await service.get_bars("AAPL", start, end)

        assert result["bars"][0]["close"] == pytest.approx(100.0)
        cached = store.read_range("AAPL", start, end)
        assert [row["close"] for row in cached] == [200.0, 200.0, 200.0]

    async def test_actions_are_persisted_during_backfill(
        self,
        service: MarketDataService,
        actions_store: CorporateActionsStore,
        today: date,
    ):
        end = today - timedelta(days=1)
        start = end - timedelta(days=4)
        ex_date = start + timedelta(days=2)
        service._polygon.fetch_splits = AsyncMock(
            return_value=[
                {
                    "execution_date": ex_date.isoformat(),
                    "split_from": 1.0,
                    "split_to": 2.0,
                }
            ]
        )

        await service.get_bars("AAPL", start, end)

        stored = actions_store.read("AAPL", start, end)
        assert len(stored) == 1
        assert stored[0]["kind"] == "split"
        assert stored[0]["date"] == ex_date

    async def test_lookback_bars_are_not_returned(
        self,
        service: MarketDataService,
        store: MarketBarsStore,
        today: date,
    ):
        end = today - timedelta(days=1)
        start = end - timedelta(days=4)
        store.write_bars(
            "AAPL",
            [_bar(start - timedelta(days=1))] + [_bar(start + timedelta(days=i)) for i in range(5)],
        )

        result = await service.get_bars("AAPL", start, end)

        dates = [b["date"] for b in result["bars"]]
        assert dates == [start + timedelta(days=i) for i in range(5)]


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

    async def test_refreshes_recent_corporate_actions(
        self, service: MarketDataService, actions_store: CorporateActionsStore, today: date
    ):
        yesterday = today - timedelta(days=1)
        service._polygon.fetch_daily_bars = AsyncMock(
            return_value=[_polygon_bar(_ts_for_ny(yesterday))]
        )
        service._polygon.fetch_dividends = AsyncMock(
            return_value=[
                {
                    "ex_dividend_date": yesterday.isoformat(),
                    "cash_amount": 0.25,
                }
            ]
        )

        await service.backfill_yesterday("AAPL")

        stored = actions_store.read("AAPL", yesterday, today)
        assert len(stored) == 1
        assert stored[0]["kind"] == "dividend"
        assert stored[0]["cash_amount"] == pytest.approx(0.25)
