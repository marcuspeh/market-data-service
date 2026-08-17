"""Tests for the parquet-backed ConstituentsStore and the
ConstituentsService read/write flow."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from app.services.constituents_service import (
    ConstituentsService,
    SnapshotNotFoundError,
    UnsupportedSymbolError,
)
from app.services.constituents_store import (
    ConstituentsNotFoundError,
    ConstituentsStore,
)


@pytest.fixture
def store(tmp_path: Path) -> ConstituentsStore:
    return ConstituentsStore(tmp_path)


class TestLayout:
    def test_path_for_per_year(self, store: ConstituentsStore):
        p = store.path_for("SPY", 2024)
        assert p.name == "2024.parquet"
        assert p.parent.name == "SPY"
        assert p.parent.parent == store._base_dir

    def test_list_years_empty(self, store: ConstituentsStore):
        assert store.list_years("SPY") == []

    def test_list_years_after_writes(self, store: ConstituentsStore):
        store.write_snapshot("SPY", date(2024, 6, 1), ["NVDA"])
        store.write_snapshot("SPY", date(2025, 6, 1), ["NVDA"])
        store.write_snapshot("SPY", date(2026, 6, 1), ["NVDA"])
        store.write_snapshot("MSFT", date(2024, 6, 1), ["XYZ"])
        assert store.list_years("SPY") == [2024, 2025, 2026]
        assert store.list_years("MSFT") == [2024]
        assert store.list_years("QQQ") == []

    def test_write_creates_year_subdir(self, store: ConstituentsStore):
        store.write_snapshot("SPY", date(2026, 8, 12), ["NVDA"])
        assert store._base_dir.joinpath("SPY").is_dir()
        assert (store._base_dir / "SPY" / "2026.parquet").exists()


class TestWriteRead:
    def test_round_trip_single_snapshot(self, store: ConstituentsStore):
        store.write_snapshot(
            "SPY", date(2026, 8, 12), ["NVDA", "AAPL", "MSFT"]
        )
        assert store.read_snapshot("SPY", date(2026, 8, 12)) == [
            "NVDA",
            "AAPL",
            "MSFT",
        ]

    def test_preserves_history_across_writes(self, store: ConstituentsStore):
        store.write_snapshot("SPY", date(2026, 8, 11), ["NVDA", "AAPL"])
        store.write_snapshot(
            "SPY", date(2026, 8, 12), ["NVDA", "AAPL", "GOOG"]
        )
        assert store.list_snapshot_dates("SPY") == [
            date(2026, 8, 11),
            date(2026, 8, 12),
        ]
        assert store.read_snapshot("SPY", date(2026, 8, 11)) == [
            "NVDA",
            "AAPL",
        ]
        assert store.read_snapshot("SPY", date(2026, 8, 12)) == [
            "NVDA",
            "AAPL",
            "GOOG",
        ]

    def test_rewrite_same_date_replaces_old_rows(
        self, store: ConstituentsStore
    ):
        store.write_snapshot("SPY", date(2026, 8, 12), ["NVDA", "AAPL"])
        store.write_snapshot("SPY", date(2026, 8, 12), ["GOOG"])
        assert store.read_snapshot("SPY", date(2026, 8, 12)) == ["GOOG"]

    def test_read_missing_date_raises(self, store: ConstituentsStore):
        store.write_snapshot("SPY", date(2026, 8, 11), ["NVDA"])
        with pytest.raises(ConstituentsNotFoundError):
            store.read_snapshot("SPY", date(2026, 8, 12))

    def test_read_missing_year_raises(self, store: ConstituentsStore):
        with pytest.raises(ConstituentsNotFoundError):
            store.read_snapshot("SPY", date(2026, 8, 12))

    def test_has_snapshot(self, store: ConstituentsStore):
        store.write_snapshot("SPY", date(2026, 8, 12), ["NVDA"])
        assert store.has_snapshot("SPY", date(2026, 8, 12))
        assert not store.has_snapshot("SPY", date(2026, 8, 13))
        assert not store.has_snapshot("QQQ", date(2026, 8, 12))

    def test_path_isolation_per_ticker(self, store: ConstituentsStore):
        store.write_snapshot("SPY", date(2026, 8, 12), ["X"])
        store.write_snapshot("QQQ", date(2026, 8, 12), ["Y"])
        assert store.read_snapshot("SPY", date(2026, 8, 12)) == ["X"]
        assert store.read_snapshot("QQQ", date(2026, 8, 12)) == ["Y"]

    def test_parquet_file_contains_only_expected_columns(
        self, store: ConstituentsStore
    ):
        store.write_snapshot("SPY", date(2026, 8, 12), ["NVDA", "AAPL"])
        df = pd.read_parquet(store.path_for("SPY", 2026))
        assert set(df.columns.tolist()) == {"date", "ticker"}


class TestReadRange:
    def test_returns_dict_of_snapshots(self, store: ConstituentsStore):
        store.write_snapshot("SPY", date(2024, 1, 15), ["A"])
        store.write_snapshot("SPY", date(2025, 6, 1), ["B"])
        store.write_snapshot("SPY", date(2026, 8, 12), ["C"])

        result = store.read_range("SPY", date(2024, 1, 1), date(2026, 12, 31))
        assert result == {
            date(2024, 1, 15): ["A"],
            date(2025, 6, 1): ["B"],
            date(2026, 8, 12): ["C"],
        }

    def test_filters_by_window(self, store: ConstituentsStore):
        store.write_snapshot("SPY", date(2024, 1, 15), ["old"])
        store.write_snapshot("SPY", date(2025, 6, 1), ["win"])

        result = store.read_range("SPY", date(2025, 1, 1), date(2025, 12, 31))
        assert set(result.keys()) == {date(2025, 6, 1)}

    def test_empty_window_returns_empty_dict(
        self, store: ConstituentsStore
    ):
        store.write_snapshot("SPY", date(2026, 8, 12), ["NVDA"])
        assert (
            store.read_range("SPY", date(2030, 1, 1), date(2031, 1, 1))
            == {}
        )


class TestYearPartitioning:
    def test_writes_partition_by_year(self, store: ConstituentsStore):
        store.write_snapshot(
            "SPY",
            date(2024, 6, 1),
            ["X-2024"],
        )
        store.write_snapshot(
            "SPY",
            date(2025, 6, 1),
            ["X-2025"],
        )
        store.write_snapshot(
            "SPY",
            date(2026, 6, 1),
            ["X-2026"],
        )
        assert sorted(store.list_years("SPY")) == [2024, 2025, 2026]

    def test_writes_to_different_years_dont_touch_each_other(
        self, store: ConstituentsStore
    ):
        store.write_snapshot(
            "SPY", date(2024, 6, 1), ["NVDA-2024"]
        )
        store.write_snapshot(
            "SPY", date(2025, 6, 1), ["NVDA-2025"]
        )
        # Read just 2024 — 2025 file untouched.
        assert store.read_snapshot(
            "SPY", date(2024, 6, 1)
        ) == ["NVDA-2024"]
        assert store.read_snapshot(
            "SPY", date(2025, 6, 1)
        ) == ["NVDA-2025"]


class TestServiceRead:
    def test_returns_tickers_list_with_metadata(
        self, store: ConstituentsStore
    ):
        store.write_snapshot("SPY", date(2026, 8, 12), ["NVDA", "AAPL"])
        service = ConstituentsService(store=store)
        result = service.get_constituents("SPY", date(2026, 8, 12))

        assert result == {
            "symbol": "SPY",
            "date": "2026-08-12",
            "source": "parquet",
            "constituents": ["NVDA", "AAPL"],
        }

    def test_unknown_symbol_raises(self, store: ConstituentsStore):
        service = ConstituentsService(store=store)
        with pytest.raises(UnsupportedSymbolError, match="XLK"):
            service.get_constituents("XLK", date(2026, 8, 12))

    def test_missing_snapshot_raises_snapshot_not_found(
        self, store: ConstituentsStore
    ):
        service = ConstituentsService(store=store)
        with pytest.raises(SnapshotNotFoundError, match="SPY"):
            service.get_constituents("SPY", date(2026, 8, 12))


def _holding(ticker: str) -> dict:
    """Upstream-shape record (the fetcher still returns name/weight)."""
    return {"ticker": ticker, "name": ticker + " Inc", "weight": 1.0}


class TestServiceRefresh:
    async def test_refresh_stores_only_tickers(
        self, store: ConstituentsStore, monkeypatch: pytest.MonkeyPatch
    ):
        async def fake_fetch(symbol: str):
            return [
                _holding("NVDA"),
                _holding("AAPL"),
                _holding("MSFT"),
            ]

        from app.services import constituents_service as svc_module
        monkeypatch.setattr(svc_module, "fetch_etf_constituents", fake_fetch)

        service = ConstituentsService(store=store)
        snap_date = date(2026, 8, 12)
        count = await service.refresh_symbol("SPY", snap_date)

        assert count == 3
        assert store.read_snapshot("SPY", snap_date) == [
            "NVDA",
            "AAPL",
            "MSFT",
        ]

    async def test_refresh_unknown_symbol_raises(
        self, store: ConstituentsStore
    ):
        service = ConstituentsService(store=store)
        with pytest.raises(UnsupportedSymbolError):
            await service.refresh_symbol("XLK", date(2026, 8, 12))

    async def test_refresh_all_iterates_supported_symbols(
        self, store: ConstituentsStore, monkeypatch: pytest.MonkeyPatch
    ):
        calls: list[str] = []

        async def fake_fetch(symbol: str):
            calls.append(symbol)
            return [_holding("X")]

        from app.services import constituents_service as svc_module
        monkeypatch.setattr(svc_module, "fetch_etf_constituents", fake_fetch)

        service = ConstituentsService(store=store)
        results = await service.refresh_all(date(2026, 8, 12))

        from app.services.constituents_fetcher import ETF_REGISTRY
        assert set(calls) == set(ETF_REGISTRY)
        assert set(results) == set(ETF_REGISTRY)
        assert all(v >= 1 for v in results.values())

    async def test_refresh_all_isolates_failures(
        self, store: ConstituentsStore, monkeypatch: pytest.MonkeyPatch
    ):
        async def fake_fetch(symbol: str):
            if symbol == "QQQ":
                raise RuntimeError("upstream down")
            return [_holding("X")]

        from app.services import constituents_service as svc_module
        monkeypatch.setattr(svc_module, "fetch_etf_constituents", fake_fetch)

        service = ConstituentsService(store=store)
        results = await service.refresh_all(date(2026, 8, 12))

        assert results["QQQ"] == -1
        assert all(v > 0 for k, v in results.items() if k != "QQQ")