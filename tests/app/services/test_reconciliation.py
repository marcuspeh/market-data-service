"""Tests for corporate-action reconciliation and cache extension."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.clients.polygon import PolygonError
from app.config.settings import Settings
from app.services.corporate_actions_store import CorporateActionsStore
from app.services.market_bars_store import MarketBarsStore
from app.services.market_data_service import MarketDataService
from tests.app.services.test_market_data_service import _bar
from tests.conftest import FakeSettings


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


def _cached_bar(d: date, close: float) -> dict[str, Any]:
    return _bar(d, close=close)


class TestReconcileTicker:
    async def test_no_cached_data_is_a_noop(
        self, service: MarketDataService, today: date
    ):
        result = await service.reconcile_ticker("AAPL")

        assert result["status"] == "no_cached_data"
        assert result["actions_fetched"] == 0
        assert result["actions_stored"] == 0
        assert result["bars_written"] == 0
        service._polygon.fetch_splits.assert_not_called()
        service._polygon.fetch_dividends.assert_not_called()
        service._polygon.fetch_daily_bars.assert_not_called()

    async def test_synced_when_cache_reaches_yesterday(
        self, service: MarketDataService, store: MarketBarsStore, today: date
    ):
        end = today - timedelta(days=1)
        start = end - timedelta(days=4)
        store.write_bars(
            "AAPL",
            [_cached_bar(start + timedelta(days=i), 100.0) for i in range(5)],
        )

        result = await service.reconcile_ticker("AAPL")

        assert result["status"] == "synced"
        assert result["bars_written"] == 0
        # Nothing left to backfill: the newest cached bar is yesterday.
        service._polygon.fetch_daily_bars.assert_not_called()
        # Actions were requested for the cached window.
        args = service._polygon.fetch_splits.call_args.args
        assert args == ("AAPL", start, end)

    async def test_persists_fetched_actions(
        self,
        service: MarketDataService,
        store: MarketBarsStore,
        actions_store: CorporateActionsStore,
        today: date,
    ):
        end = today - timedelta(days=1)
        start = end - timedelta(days=4)
        store.write_bars("AAPL", [_cached_bar(start, 100.0)])

        service._polygon.fetch_splits = AsyncMock(
            return_value=[
                {
                    "execution_date": (start + timedelta(days=1)).isoformat(),
                    "split_from": 1.0,
                    "split_to": 2.0,
                }
            ]
        )
        service._polygon.fetch_dividends = AsyncMock(
            return_value=[
                {
                    "ex_dividend_date": (start + timedelta(days=2)).isoformat(),
                    "cash_amount": 0.5,
                }
            ]
        )

        result = await service.reconcile_ticker("AAPL")

        assert result["status"] == "synced"
        assert result["actions_fetched"] == 2
        assert result["actions_stored"] == 2
        assert len(actions_store.read("AAPL", start, end)) == 2

    async def test_extends_the_cache_to_yesterday(
        self, service: MarketDataService, store: MarketBarsStore, today: date
    ):
        last = today - timedelta(days=4)
        store.write_bars("AAPL", [_cached_bar(last, 100.0)])

        service._polygon.fetch_daily_bars = AsyncMock(
            return_value=[
                _cached_bar(today - timedelta(days=i), 100.0) for i in range(4)
            ]
        )

        result = await service.reconcile_ticker("AAPL")

        assert result["status"] == "synced"
        # today-3 .. today, of which today is intraday and not persisted.
        assert result["bars_written"] == 3
        args = service._polygon.fetch_daily_bars.call_args.args
        assert args == ("AAPL", last + timedelta(days=1), today - timedelta(days=1))
        assert store.read_range("AAPL", today, today) == []

    async def test_action_fetch_error_keeps_cache(
        self, service: MarketDataService, store: MarketBarsStore, today: date
    ):
        end = today - timedelta(days=1)
        store.write_bars("AAPL", [_cached_bar(end, 100.0)])

        service._polygon.fetch_splits = AsyncMock(
            side_effect=PolygonError("polygon boom")
        )

        result = await service.reconcile_ticker("AAPL")

        assert result["status"] == "error"
        assert result["error"] == "polygon boom"
        assert result["actions_stored"] == 0
        service._polygon.fetch_daily_bars.assert_not_called()
        assert len(store.read_range("AAPL", end, end)) == 1

    async def test_bar_backfill_error_keeps_cache_and_actions(
        self,
        service: MarketDataService,
        store: MarketBarsStore,
        actions_store: CorporateActionsStore,
        today: date,
    ):
        last = today - timedelta(days=4)
        store.write_bars("AAPL", [_cached_bar(last, 100.0)])

        service._polygon.fetch_splits = AsyncMock(
            return_value=[
                {
                    "execution_date": last.isoformat(),
                    "split_from": 1.0,
                    "split_to": 2.0,
                }
            ]
        )
        service._polygon.fetch_daily_bars = AsyncMock(
            side_effect=PolygonError("bars boom")
        )

        result = await service.reconcile_ticker("AAPL")

        assert result["status"] == "error"
        assert result["actions_stored"] == 1
        assert result["bars_written"] == 0
        assert len(actions_store.read("AAPL", last, last)) == 1
        assert len(store.read_range("AAPL", last, last)) == 1

    async def test_empty_upstream_response_keeps_cache(
        self, service: MarketDataService, store: MarketBarsStore, today: date
    ):
        last = today - timedelta(days=4)
        store.write_bars("AAPL", [_cached_bar(last, 100.0)])
        service._polygon.fetch_daily_bars = AsyncMock(return_value=[])

        result = await service.reconcile_ticker("AAPL")

        assert result["status"] == "synced"
        assert result["bars_written"] == 0
        assert len(store.read_range("AAPL", last, last)) == 1


class TestReconcileAll:
    async def test_reconciles_every_cached_ticker(
        self, service: MarketDataService, store: MarketBarsStore, today: date
    ):
        end = today - timedelta(days=1)
        store.write_bars("AAPL", [_cached_bar(end, 100.0)])
        store.write_bars("MSFT", [_cached_bar(end, 200.0)])

        results = await service.reconcile_all()

        assert set(results) == {"AAPL", "MSFT"}
        assert results["AAPL"]["status"] == "synced"
        assert results["MSFT"]["status"] == "synced"

    async def test_one_failing_ticker_does_not_stop_the_others(
        self, service: MarketDataService, store: MarketBarsStore, today: date
    ):
        end = today - timedelta(days=1)
        store.write_bars("AAPL", [_cached_bar(end, 100.0)])
        store.write_bars("MSFT", [_cached_bar(end, 200.0)])

        async def side_effect(ticker, *_args, **_kwargs):
            if ticker == "AAPL":
                raise PolygonError("polygon boom")
            return []

        service._polygon.fetch_splits = AsyncMock(side_effect=side_effect)

        results = await service.reconcile_all()

        assert results["AAPL"]["status"] == "error"
        assert results["MSFT"]["status"] == "synced"
