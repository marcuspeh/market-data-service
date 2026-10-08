"""Tests for the parquet-backed ConstituentsStore and the
ConstituentsService read/write flow."""

from __future__ import annotations

from datetime import date, timedelta
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


class TestBackfillGapsInRecentWindow:
    """Tests for
    :meth:`ConstituentsService.backfill_gaps_in_recent_window`."""

    def _write(self, store: ConstituentsStore, symbol: str, d: date, rows: list[str]):
        store.write_snapshot(symbol, d, rows)

    def test_fills_single_t1_gap_from_previous_day(
        self, store: ConstituentsStore
    ):
        from app.services.constituents_fetcher import ETF_REGISTRY

        today = date(2026, 8, 13)  # Thursday
        t1 = date(2026, 8, 12)  # Wednesday (gap)
        t2 = date(2026, 8, 11)  # Tuesday (source)

        for symbol in ETF_REGISTRY:
            self._write(store, symbol, t2, ["AAPL", "NVDA"])
            # t1 missing for all symbols

        service = ConstituentsService(store=store)
        results = service.backfill_gaps_in_recent_window(today=today)

        assert set(results) == set(ETF_REGISTRY)
        assert all(r == "backfilled:1_gaps" for r in results.values())
        for symbol in ETF_REGISTRY:
            assert store.read_snapshot(symbol, t1) == ["AAPL", "NVDA"]
            # Source untouched.
            assert store.read_snapshot(symbol, t2) == ["AAPL", "NVDA"]

    def test_fills_multiple_gaps_in_window(
        self, store: ConstituentsStore
    ):
        """A contiguous run of gaps in the window is filled in
        chronological order — each gap pulls from the most recent
        prior snapshot, which after the previous fill may itself be a
        just-backfilled row."""
        from app.services.constituents_fetcher import ETF_REGISTRY

        today = date(2026, 8, 13)
        # Window = [2026-08-06, 2026-08-12].
        # Anchor the chain with a source at 2026-08-05, so all 7
        # window-days get filled (08-06 from 08-05, then 08-07 from
        # 08-06, etc. — each prior fill becomes the next gap's source).
        for symbol in ETF_REGISTRY:
            self._write(store, symbol, date(2026, 8, 5), ["OLD"])

        service = ConstituentsService(store=store)
        results = service.backfill_gaps_in_recent_window(today=today)

        assert all(r == "backfilled:7_gaps" for r in results.values())
        for symbol in ETF_REGISTRY:
            for offset in range(1, 8):
                # Every window day now has a snapshot, and they all
                # trace back to the anchor at 08-05.
                assert store.read_snapshot(
                    symbol, today - timedelta(days=offset)
                ) is not None

    def test_does_not_overwrite_existing_data(
        self, store: ConstituentsStore
    ):
        from app.services.constituents_fetcher import ETF_REGISTRY

        today = date(2026, 8, 13)
        # Full coverage in the 7-day window — no gaps.
        for symbol in ETF_REGISTRY:
            for offset in range(1, 8):
                self._write(
                    store, symbol, today - timedelta(days=offset), [f"DAY-{offset}"]
                )

        service = ConstituentsService(store=store)
        results = service.backfill_gaps_in_recent_window(today=today)

        assert all(r == "already_present" for r in results.values())
        # No real rows were rewritten.
        for symbol in ETF_REGISTRY:
            for offset in range(1, 8):
                assert store.read_snapshot(
                    symbol, today - timedelta(days=offset)
                ) == [f"DAY-{offset}"]

    def test_today_is_not_scanned(
        self, store: ConstituentsStore
    ):
        """Today is excluded from the window — refresh_all is responsible
        for writing today's snapshot."""
        from app.services.constituents_fetcher import ETF_REGISTRY

        today = date(2026, 8, 13)
        # Write a row for today only; gap-fill must not touch it.
        for symbol in ETF_REGISTRY:
            self._write(store, symbol, today, ["TODAY"])
        # Fill the prior 7 days so only today is sparse.
        for offset in range(1, 8):
            for symbol in ETF_REGISTRY:
                self._write(store, symbol, today - timedelta(days=offset), [f"D-{offset}"])

        service = ConstituentsService(store=store)
        results = service.backfill_gaps_in_recent_window(today=today)

        assert all(r == "already_present" for r in results.values())

    def test_window_is_exactly_seven_days(
        self, store: ConstituentsStore
    ):
        """Day T-8 (outside the 7-day window) being absent must NOT
        be reported as a gap and must NOT be filled."""
        from app.services.constituents_fetcher import ETF_REGISTRY

        today = date(2026, 8, 13)
        for symbol in ETF_REGISTRY:
            # Fill T-7..T-1 (the whole window) for each symbol.
            for offset in range(1, 8):
                self._write(
                    store, symbol, today - timedelta(days=offset), [f"D-{offset}"]
                )
            # T-8 has no data — must remain unfilled.
            assert not store.has_snapshot(symbol, today - timedelta(days=8))

        service = ConstituentsService(store=store)
        results = service.backfill_gaps_in_recent_window(today=today)

        assert all(r == "already_present" for r in results.values())
        for symbol in ETF_REGISTRY:
            with pytest.raises(ConstituentsNotFoundError):
                store.read_snapshot(symbol, today - timedelta(days=8))

    def test_uses_older_source_when_window_fully_empty(
        self, store: ConstituentsStore
    ):
        """All 7 days in the window are gaps, but a snapshot from
        further back exists → that older snapshot fills every gap."""
        from app.services.constituents_fetcher import ETF_REGISTRY

        today = date(2026, 8, 13)
        for symbol in ETF_REGISTRY:
            self._write(store, symbol, today - timedelta(days=30), ["OLD-BUT-VALID"])

        service = ConstituentsService(store=store)
        results = service.backfill_gaps_in_recent_window(today=today)

        assert all(r == "backfilled:7_gaps" for r in results.values())
        for symbol in ETF_REGISTRY:
            for offset in range(1, 8):
                assert store.read_snapshot(
                    symbol, today - timedelta(days=offset)
                ) == ["OLD-BUT-VALID"]

    def test_no_source_data_when_no_prior_history(
        self, store: ConstituentsStore
    ):
        from app.services.constituents_fetcher import ETF_REGISTRY

        today = date(2026, 8, 13)
        service = ConstituentsService(store=store)
        results = service.backfill_gaps_in_recent_window(today=today)

        assert all(r == "no_source_data" for r in results.values())
        assert set(results) == set(ETF_REGISTRY)

    def test_handles_mixed_symbol_states(
        self, store: ConstituentsStore
    ):
        from app.services.constituents_fetcher import ETF_REGISTRY

        symbols = list(ETF_REGISTRY)
        sym_full, sym_gap, sym_no_history = symbols[0], symbols[1], symbols[2]

        today = date(2026, 8, 13)
        for offset in range(1, 8):
            self._write(store, sym_full, today - timedelta(days=offset), [f"F-{offset}"])

        # sym_gap: 7 days of gaps, but an older source exists
        self._write(store, sym_gap, today - timedelta(days=20), ["SOURCE"])

        # sym_no_history: nothing at all
        service = ConstituentsService(store=store)
        results = service.backfill_gaps_in_recent_window(today=today)

        assert results[sym_full] == "already_present"
        assert results[sym_gap] == "backfilled:7_gaps"
        assert results[sym_no_history] == "no_source_data"

    def test_is_idempotent(
        self, store: ConstituentsStore
    ):
        """Running the scan twice in a row produces the same end state
        and the second call reports everything as already_present."""
        from app.services.constituents_fetcher import ETF_REGISTRY

        today = date(2026, 8, 13)
        for symbol in ETF_REGISTRY:
            self._write(store, symbol, today - timedelta(days=20), ["BASE"])

        service = ConstituentsService(store=store)
        first = service.backfill_gaps_in_recent_window(today=today)
        second = service.backfill_gaps_in_recent_window(today=today)

        assert all(r == "backfilled:7_gaps" for r in first.values())
        assert all(r == "already_present" for r in second.values())

    def test_uses_ny_date_when_today_omitted(
        self, store: ConstituentsStore, monkeypatch: pytest.MonkeyPatch
    ):
        fixed_today = date(2026, 8, 13)
        for offset in range(1, 8):
            store.write_snapshot("SPY", fixed_today - timedelta(days=offset), [f"DAY-{offset}"])

        class _FakeSettings:
            @staticmethod
            def now_ny_date():
                return fixed_today

        from app.services import constituents_service as svc_module
        monkeypatch.setattr(svc_module, "get_settings", _FakeSettings)

        service = ConstituentsService(store=store)
        results = service.backfill_gaps_in_recent_window()

        assert results["SPY"] == "already_present"

    def test_scheduler_shim_routes_to_gap_scan(
        self, store: ConstituentsStore
    ):
        """The legacy backfill_missing_t1_from_t2 entry point still
        works (the scheduler calls it)."""
        from app.services.constituents_fetcher import ETF_REGISTRY

        today = date(2026, 8, 13)
        for symbol in ETF_REGISTRY:
            self._write(store, symbol, today - timedelta(days=5), ["PREV"])

        service = ConstituentsService(store=store)
        results = service.backfill_missing_t1_from_t2(today=today)

        # The window covers T-1..T-7. The anchor at T-5 fills T-5..T-1
        # (4 dates), but T-6 and T-7 have no source and stay empty.
        # The action string reports only the count of successful fills.
        assert all(r == "backfilled:4_gaps" for r in results.values())