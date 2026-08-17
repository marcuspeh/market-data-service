"""Tests for the per-ticker-per-year MarketBarsStore."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

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
        ts = lambda d: int(
            datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000
        )
        store.write_bars("AAPL", [_bar(ts(date(2024, 6, 1)))])
        store.write_bars("AAPL", [_bar(ts(date(2025, 6, 1)))])
        store.write_bars("MSFT", [_bar(ts(date(2024, 6, 1)))])
        assert store.list_years("AAPL") == [2024, 2025]
        assert store.list_years("MSFT") == [2024]
        assert store.list_years("QQQ") == []


class TestWriteRead:
    def test_round_trip_single_bar(self, store: MarketBarsStore):
        ts = int(
            datetime(2024, 6, 1, tzinfo=timezone.utc).timestamp() * 1000
        )
        store.write_bars("AAPL", [_bar(ts)])
        result = store.read_range("AAPL", date(2024, 6, 1), date(2024, 6, 1))
        assert len(result) == 1
        assert result[0]["ticker"] == "AAPL"
        assert result[0]["open"] == 100.0

    def test_year_partitioning(self, store: MarketBarsStore):
        rows = []
        for y in (2023, 2024, 2025):
            ts = int(datetime(y, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)
            rows.append(_bar(ts))
        store.write_bars("AAPL", rows)

        # 3 yearly files exist.
        assert sorted(store.list_years("AAPL")) == [2023, 2024, 2025]

        # Read across the boundary spans all three files.
        all_rows = store.read_range("AAPL", date(2023, 6, 1), date(2025, 6, 1))
        assert len(all_rows) == 3

    def test_rows_are_sorted_by_date_within_a_file(self, store: MarketBarsStore):
        # Write the same year's bars out of order.
        rows = [
            _bar(int(datetime(2024, 6, 3, tzinfo=timezone.utc).timestamp() * 1000)),
            _bar(int(datetime(2024, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)),
            _bar(int(datetime(2024, 6, 2, tzinfo=timezone.utc).timestamp() * 1000)),
        ]
        store.write_bars("AAPL", rows)

        result = store.read_range("AAPL", date(2024, 6, 1), date(2024, 6, 3))
        days = [r["date"] for r in result]
        assert days == sorted(days)

    def test_rewrite_same_date_replaces_old_row(self, store: MarketBarsStore):
        ts1 = int(datetime(2024, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)
        ts2 = int(datetime(2024, 6, 1, tzinfo=timezone.utc).timestamp() * 1000) + 1000
        # First write — one bar at price 100
        store.write_bars("AAPL", [{**{"t": ts1, "o": 100.0, "h": 101.0, "l": 99.0,
                                       "c": 100.5, "v": 1000.0, "vw": 100.2, "n": 10}}])
        # Second write — different close for the same date
        store.write_bars("AAPL", [{**{"t": ts2, "o": 200.0, "h": 201.0, "l": 199.0,
                                       "c": 200.5, "v": 2000.0, "vw": 200.2, "n": 20}}])
        result = store.read_range("AAPL", date(2024, 6, 1), date(2024, 6, 1))
        assert len(result) == 1
        assert result[0]["close"] == 200.5

    def test_out_of_range_read_filtered(self, store: MarketBarsStore):
        ts = int(datetime(2024, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)
        store.write_bars("AAPL", [_bar(ts)])

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
                ts = int(datetime(y, m, 1, tzinfo=timezone.utc).timestamp() * 1000)
                rows.append(_bar(ts))
        store.write_bars("AAPL", rows)

        # Window only in 2024.
        in_range = store.read_range("AAPL", date(2024, 1, 1), date(2024, 12, 31))
        assert len(in_range) == 3

        # Window includes 2024 + parts of 2023 and 2025.
        spanning = store.read_range("AAPL", date(2023, 6, 1), date(2025, 6, 30))
        assert len(spanning) == 7  # 2 in 2023, 3 in 2024, 2 in 2025

    def test_missing_years_contribute_nothing(self, store: MarketBarsStore):
        # Only 2024 is written.
        ts = int(datetime(2024, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)
        store.write_bars("AAPL", [_bar(ts)])

        # 2023 and 2025 files don't exist — no error, no rows.
        result = store.read_range("AAPL", date(2023, 1, 1), date(2025, 12, 31))
        assert len(result) == 1