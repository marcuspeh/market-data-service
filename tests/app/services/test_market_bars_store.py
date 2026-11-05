"""Tests for the per-ticker-per-year MarketBarsStore."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from app.config.settings import NY_TZ
from app.services.market_bars_store import MarketBarsStore


def _bar(timestamp: int) -> dict:
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


def _ts_for_ny(d: date) -> int:
    """Epoch-ms for an NY-midnight timestamp on ``d``.

    Used so that the bar's stored ``date`` column is exactly ``d`` in
    the parquet (which now reasons in ET).
    """
    return int(
        datetime(d.year, d.month, d.day, tzinfo=NY_TZ)
        .astimezone(timezone.utc)
        .timestamp()
        * 1000
    )


def _bar_with_close(timestamp: int, close: float) -> dict:
    """A bar with a specific close price."""
    bar = _bar(timestamp)
    bar["c"] = close
    return bar


@pytest.fixture
def store(tmp_path: Path) -> MarketBarsStore:
    return MarketBarsStore(tmp_path / "market")


class TestLayout:
    def test_path_for(self, store: MarketBarsStore):
        p = store.path_for("AAPL", 2024)
        assert p.name == "2024.parquet"
        assert p.parent.name == "AAPL"

    def test_list_years_empty(self, store: MarketBarsStore):
        assert store.list_years("AAPL") == []

    def test_list_years_after_write(self, store: MarketBarsStore):
        store.write_bars("AAPL", [_bar(_ts_for_ny(date(2024, 6, 1)))])
        store.write_bars("AAPL", [_bar(_ts_for_ny(date(2025, 6, 1)))])
        store.write_bars("MSFT", [_bar(_ts_for_ny(date(2024, 6, 1)))])
        assert store.list_years("AAPL") == [2024, 2025]
        assert store.list_years("MSFT") == [2024]
        assert store.list_years("QQQ") == []


class TestWriteRead:
    def test_round_trip_single_bar(self, store: MarketBarsStore):
        store.write_bars("AAPL", [_bar(_ts_for_ny(date(2024, 6, 1)))])
        result = store.read_range("AAPL", date(2024, 6, 1), date(2024, 6, 1))
        assert len(result) == 1
        assert result[0]["ticker"] == "AAPL"
        assert result[0]["open"] == 100.0

    def test_year_partitioning(self, store: MarketBarsStore):
        rows = []
        for y in (2023, 2024, 2025):
            rows.append(_bar(_ts_for_ny(date(y, 6, 1))))
        store.write_bars("AAPL", rows)

        # 3 yearly files exist.
        assert sorted(store.list_years("AAPL")) == [2023, 2024, 2025]

        # Read across the boundary spans all three files.
        all_rows = store.read_range("AAPL", date(2023, 6, 1), date(2025, 6, 1))
        assert len(all_rows) == 3

    def test_rows_are_sorted_by_date_within_a_file(self, store: MarketBarsStore):
        # Write the same year's bars out of order.
        rows = [
            _bar(_ts_for_ny(date(2024, 6, 3))),
            _bar(_ts_for_ny(date(2024, 6, 1))),
            _bar(_ts_for_ny(date(2024, 6, 2))),
        ]
        store.write_bars("AAPL", rows)

        result = store.read_range("AAPL", date(2024, 6, 1), date(2024, 6, 3))
        days = [r["date"] for r in result]
        assert days == sorted(days)

    def test_rewrite_same_date_replaces_old_row(self, store: MarketBarsStore):
        ts1 = _ts_for_ny(date(2024, 6, 1))
        ts2 = ts1 + 1000
        # First write — one bar at price 100
        store.write_bars(
            "AAPL",
            [{"t": ts1, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.5,
              "v": 1000.0, "vw": 100.2, "n": 10}],
        )
        # Second write — different close for the same date
        store.write_bars(
            "AAPL",
            [{"t": ts2, "o": 200.0, "h": 201.0, "l": 199.0, "c": 200.5,
              "v": 2000.0, "vw": 200.2, "n": 20}],
        )
        result = store.read_range("AAPL", date(2024, 6, 1), date(2024, 6, 1))
        assert len(result) == 1
        assert result[0]["close"] == 200.5

    def test_out_of_range_read_filtered(self, store: MarketBarsStore):
        store.write_bars("AAPL", [_bar(_ts_for_ny(date(2024, 6, 1)))])

        # Read before the date → empty
        assert store.read_range("AAPL", date(2024, 1, 1), date(2024, 1, 5)) == []
        # Read after the date → empty
        assert store.read_range("AAPL", date(2025, 1, 1), date(2025, 5, 5)) == []


class TestReadRangeAcrossYears:
    def test_filters_by_window(self, store: MarketBarsStore):
        """The cross-year read should respect the (start, end) window."""
        rows = []
        for y in (2023, 2024, 2025):
            for m in (1, 6, 12):
                rows.append(_bar(_ts_for_ny(date(y, m, 1))))
        store.write_bars("AAPL", rows)

        # Window only in 2024.
        in_range = store.read_range("AAPL", date(2024, 1, 1), date(2024, 12, 31))
        assert len(in_range) == 3

        # Window includes 2024 + parts of 2023 and 2025.
        spanning = store.read_range("AAPL", date(2023, 6, 1), date(2025, 6, 30))
        assert len(spanning) == 7  # 2 in 2023, 3 in 2024, 2 in 2025

    def test_missing_years_contribute_nothing(self, store: MarketBarsStore):
        # Only 2024 is written.
        store.write_bars("AAPL", [_bar(_ts_for_ny(date(2024, 6, 1)))])

        # 2023 and 2025 files don't exist — no error, no rows.
        result = store.read_range("AAPL", date(2023, 1, 1), date(2025, 12, 31))
        assert len(result) == 1


class TestTimezoneSemantics:
    def test_bar_at_utc_midnight_stored_under_prior_ny_date(self, store: MarketBarsStore):
        """A bar whose UTC timestamp is 2026-08-22 00:30 (i.e. 2026-08-21
        20:30 ET) must be stored under the NY trading date of 2026-08-21,
        not the UTC date. Otherwise early-morning Polygon fetches would
        be misattributed to the wrong session.
        """
        # NY 2026-08-21 23:30 is UTC 2026-08-22 03:30.
        ts = _ts_for_ny(date(2026, 8, 21))
        store.write_bars("AAPL", [_bar(ts)])

        result = store.read_range("AAPL", date(2026, 8, 21), date(2026, 8, 21))
        assert len(result) == 1
        assert result[0]["date"] == date(2026, 8, 21)


class TestListTickers:
    def test_empty_when_no_data(self, store: MarketBarsStore):
        assert store.list_tickers() == []

    def test_lists_tickers_with_cached_files(self, store: MarketBarsStore):
        store.write_bars("AAPL", [_bar(_ts_for_ny(date(2024, 6, 1)))])
        store.write_bars("MSFT", [_bar(_ts_for_ny(date(2025, 6, 1)))])
        assert store.list_tickers() == ["AAPL", "MSFT"]

    def test_ignores_directories_without_parquet(self, store: MarketBarsStore):
        store.write_bars("AAPL", [_bar(_ts_for_ny(date(2024, 6, 1)))])
        (store._base_dir / "EMPTY").mkdir(parents=True, exist_ok=True)
        assert store.list_tickers() == ["AAPL"]


class TestCachedExtent:
    def test_none_when_no_rows(self, store: MarketBarsStore):
        assert store.cached_extent("AAPL") is None

    def test_returns_first_and_last_date(self, store: MarketBarsStore):
        store.write_bars("AAPL", [
            _bar_with_close(_ts_for_ny(date(2024, 6, 3)), 100.0),
            _bar_with_close(_ts_for_ny(date(2024, 6, 1)), 120.0),
        ])
        store.write_bars("AAPL", [
            _bar_with_close(_ts_for_ny(date(2025, 6, 1)), 90.0),
        ])

        first, last = store.cached_extent("AAPL")

        assert first == date(2024, 6, 1)
        assert last == date(2025, 6, 1)

    def test_before_excludes_later_dates(self, store: MarketBarsStore):
        store.write_bars("AAPL", [
            _bar_with_close(_ts_for_ny(date(2024, 6, 1)), 100.0),
            _bar_with_close(_ts_for_ny(date(2025, 6, 1)), 500.0),
        ])

        first, last = store.cached_extent("AAPL", before=date(2025, 1, 1))

        assert first == date(2024, 6, 1)
        assert last == date(2024, 6, 1)

    def test_before_excludes_everything(self, store: MarketBarsStore):
        store.write_bars("AAPL", [
            _bar_with_close(_ts_for_ny(date(2025, 6, 1)), 500.0),
        ])

        assert store.cached_extent("AAPL", before=date(2025, 1, 1)) is None


class TestCachedDates:
    def test_returns_every_cached_date_in_window(self, store: MarketBarsStore):
        store.write_bars("AAPL", [
            _bar(_ts_for_ny(date(2024, 6, 1))),
            _bar(_ts_for_ny(date(2024, 6, 3))),
            _bar(_ts_for_ny(date(2024, 6, 5))),
        ])

        dates = store.cached_dates(
            "AAPL", date(2024, 6, 1), date(2024, 6, 5)
        )

        assert dates == [date(2024, 6, 1), date(2024, 6, 3), date(2024, 6, 5)]

    def test_excludes_dates_outside_the_window(self, store: MarketBarsStore):
        store.write_bars("AAPL", [
            _bar(_ts_for_ny(date(2024, 5, 31))),
            _bar(_ts_for_ny(date(2024, 6, 1))),
            _bar(_ts_for_ny(date(2024, 6, 2))),
        ])

        dates = store.cached_dates(
            "AAPL", date(2024, 6, 1), date(2024, 6, 2)
        )

        assert dates == [date(2024, 6, 1), date(2024, 6, 2)]

    def test_empty_for_unknown_ticker(self, store: MarketBarsStore):
        assert store.cached_dates(
            "NOPE", date(2024, 6, 1), date(2024, 6, 5)
        ) == []


class TestAtomicWrites:
    def test_concurrent_writers_use_distinct_temp_files(
        self, store: MarketBarsStore, monkeypatch: pytest.MonkeyPatch
    ):
        """Two writers of the same ticker/year must not share a temp file.

        A shared temp name lets one writer rename a partially written
        file into place, corrupting the cache.
        """
        import app.services.parquet_io as parquet_io

        seen: list[Path] = []
        original = parquet_io.temp_path

        def spy(target: Path) -> Path:
            path = original(target)
            seen.append(path)
            return path

        monkeypatch.setattr(parquet_io, "temp_path", spy)

        store.write_bars("AAPL", [_bar(_ts_for_ny(date(2024, 6, 1)))])
        store.write_bars("AAPL", [_bar(_ts_for_ny(date(2024, 6, 2)))])

        assert len(seen) == 2
        assert seen[0] != seen[1]

    def test_no_temp_files_are_left_behind(self, store: MarketBarsStore):
        store.write_bars("AAPL", [_bar(_ts_for_ny(date(2024, 6, 1)))])
        leftovers = list(store.path_for("AAPL", 2024).parent.glob("*.tmp"))
        assert leftovers == []


class TestEnsureRawBasis:
    def test_fresh_cache_stamps_marker(self, store: MarketBarsStore):
        assert store.ensure_raw_basis() is False
        assert (store._base_dir / ".price_basis").read_text() == "raw"

    def test_wipes_legacy_basis_once(self, store: MarketBarsStore):
        store.write_bars("AAPL", [_bar(_ts_for_ny(date(2024, 6, 1)))])
        assert store.list_tickers() == ["AAPL"]

        assert store.ensure_raw_basis() is True
        assert store.list_tickers() == []

        # Second call is a no-op: the marker now records the raw basis.
        assert store.ensure_raw_basis() is False
        assert store.list_tickers() == []

    def test_keeps_cache_written_in_raw_basis(self, store: MarketBarsStore):
        store.ensure_raw_basis()
        store.write_bars("AAPL", [_bar(_ts_for_ny(date(2024, 6, 1)))])

        assert store.ensure_raw_basis() is False
        assert store.list_tickers() == ["AAPL"]


class TestDeleteTicker:
    def test_returns_zero_for_unknown_ticker(self, store: MarketBarsStore):
        assert store.delete_ticker("AAPL") == 0

    def test_deletes_all_years(self, store: MarketBarsStore):
        store.write_bars("AAPL", [_bar(_ts_for_ny(date(2024, 6, 1)))])
        store.write_bars("AAPL", [_bar(_ts_for_ny(date(2025, 6, 1)))])
        store.write_bars("MSFT", [_bar(_ts_for_ny(date(2025, 6, 1)))])

        removed = store.delete_ticker("AAPL")

        assert removed == 2
        assert store.list_years("AAPL") == []
        # Other tickers are untouched.
        assert store.list_years("MSFT") == [2025]