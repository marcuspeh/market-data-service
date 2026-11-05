"""Parquet-backed store for daily OHLCV bars.

Prices are stored **raw**, exactly as Polygon/Longbridge publish them.
Split/dividend back-adjustment happens at read time (see
:mod:`app.services.adjustments`), so a bar's numbers never change once
written and reconciliation only has to keep corporate actions current.

Layout (one parquet per ticker per calendar year):

    <data_dir>/market/AAPL/2024.parquet
    <data_dir>/market/AAPL/2025.parquet
    <data_dir>/market/MSFT/2024.parquet
    ...

Schema:

    date        : date32   (bar date)
    ticker      : string
    timestamp   : int64    (NY-midnight in ms)
    open/high/low/close : double
    volume      : double
    vwap        : double (nullable)
    trade_count : int64  (nullable)

Rows are always sorted ascending by ``date`` inside each file.

Reads for an arbitrary (start, end) range span one or more yearly files.
Writes are append-style: load the year's parquet (if any), drop rows
whose date matches the new bars, concatenate the new bars, sort by
date ascending, and write back atomically via a temp-file rename.

A ``.price_basis`` marker at the root records which basis the files
hold. Caches written by earlier versions stored vendor-adjusted
(split-adjusted) prices, which cannot be mixed with raw bars; the
marker lets :meth:`MarketBarsStore.ensure_raw_basis` drop those once.
"""
import math
import shutil
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from app.config.settings import ny_from_ts
from app.logging_setup import client
from app.services.parquet_io import write_parquet_atomic

log = client()

DATE_COL = "date"
TICKER_COL = "ticker"
TIMESTAMP_COL = "timestamp"
OPEN_COL = "open"
HIGH_COL = "high"
LOW_COL = "low"
CLOSE_COL = "close"
VOLUME_COL = "volume"
VWAP_COL = "vwap"
TRADE_COUNT_COL = "trade_count"

REQUIRED_COLUMNS = [
    DATE_COL,
    TICKER_COL,
    TIMESTAMP_COL,
    OPEN_COL,
    HIGH_COL,
    LOW_COL,
    CLOSE_COL,
    VOLUME_COL,
]
NULLABLE_COLUMNS = [VWAP_COL, TRADE_COUNT_COL]

#: Basis of the stored prices. Bump only if the write semantics change.
PRICE_BASIS = "raw"
#: Root-level marker holding :data:`PRICE_BASIS`.
BASIS_MARKER = ".price_basis"


class MarketBarsStore:
    def __init__(self, base_dir: str | Path):
        self._base_dir = Path(base_dir)

    def path_for(self, ticker: str, year: int) -> Path:
        return self._base_dir / ticker.upper() / f"{year}.parquet"

    def ensure_raw_basis(self) -> bool:
        """Guarantee the cache holds raw prices, wiping legacy data once.

        Returns ``True`` when stale-basis files were dropped. Older
        versions cached Polygon's split-adjusted series; mixing those
        with raw bars would silently corrupt every adjusted read, so a
        cache without the marker (or with a different one) is discarded.
        """
        marker = self._base_dir / BASIS_MARKER
        current = marker.read_text().strip() if marker.exists() else None
        if current == PRICE_BASIS:
            return False

        wiped = 0
        if self._base_dir.exists():
            for child in self._base_dir.iterdir():
                if child.is_dir():
                    shutil.rmtree(child)
                    wiped += 1

        self._base_dir.mkdir(parents=True, exist_ok=True)
        marker.write_text(PRICE_BASIS)
        if wiped:
            log.warn(
                "Discarded %d cached ticker directories: prices were "
                "written in a %r basis, cache now holds %r",
                wiped, current, PRICE_BASIS,
            )
        return wiped > 0

    def _years_for_range(self, start: date, end: date) -> list[int]:
        if end < start:
            return []
        return list(range(start.year, end.year + 1))

    def list_years(self, ticker: str) -> list[int]:
        ticker_dir = self._base_dir / ticker.upper()
        if not ticker_dir.exists():
            return []
        return sorted(
            int(p.stem)
            for p in ticker_dir.glob("*.parquet")
            if p.stem.isdigit()
        )

    def list_tickers(self) -> list[str]:
        """Tickers that have at least one cached yearly parquet file."""
        if not self._base_dir.exists():
            return []
        tickers = [
            p.name.upper()
            for p in self._base_dir.iterdir()
            if p.is_dir() and any(p.glob("*.parquet"))
        ]
        return sorted(tickers)

    def cached_extent(
        self, ticker: str, *, before: date | None = None
    ) -> tuple[date, date] | None:
        """``(first_date, last_date)`` over a ticker's cached bars.

        Returns ``None`` when the ticker has no cached rows. ``before``
        restricts the scan to bars strictly earlier than that date, so a
        stale intraday row can't skew the extent. Only the ``date``
        column is read, keeping this cheap for long histories.
        """
        first: date | None = None
        last: date | None = None

        for year in self.list_years(ticker):
            path = self.path_for(ticker, year)
            df = pd.read_parquet(path, columns=[DATE_COL])
            if df.empty:
                continue

            dates = pd.to_datetime(df[DATE_COL]).dt.date
            if before is not None:
                dates = dates[dates < before]
                if dates.empty:
                    continue

            year_first = dates.min()
            year_last = dates.max()

            first = year_first if first is None else min(first, year_first)
            last = year_last if last is None else max(last, year_last)

        if first is None or last is None:
            return None
        return first, last

    def cached_dates(
        self, ticker: str, start: date, end: date
    ) -> list[date]:
        """Every cached bar date in ``[start, end]``, ascending.

        Only the ``date`` column is read. The list is what tells a caller
        whether the cache *contiguously* covers a window — the extent
        alone can't distinguish a full history from two disjoint spans.
        """
        if end < start:
            return []

        dates: set[date] = set()
        for year in self._years_for_range(start, end):
            path = self.path_for(ticker, year)
            if not path.exists():
                continue
            df = pd.read_parquet(path, columns=[DATE_COL])
            if df.empty:
                continue
            for value in pd.to_datetime(df[DATE_COL]).dt.date:
                if start <= value <= end:
                    dates.add(value)
        return sorted(dates)

    def delete_ticker(self, ticker: str) -> int:
        """Remove every cached yearly file for ``ticker``.

        Returns the number of files deleted. Used to drop a ticker's
        cache wholesale so the next read refetches it from upstream.
        """
        ticker_dir = self._base_dir / ticker.upper()
        if not ticker_dir.exists():
            return 0

        removed = 0
        for path in ticker_dir.glob("*.parquet"):
            path.unlink()
            removed += 1

        if not any(ticker_dir.iterdir()):
            ticker_dir.rmdir()

        log.info("Deleted %d cached file(s) for %s", removed, ticker.upper())
        return removed

    def read_range(
        self,
        ticker: str,
        start: date,
        end: date,
    ) -> list[dict[str, Any]]:
        """Return cached bars for ``(ticker, start, end)``.

        Years missing from disk contribute nothing — they aren't errors.
        Caller decides what to backfill via :meth:`write_bars`.
        """
        bars: list[dict[str, Any]] = []
        for year in self._years_for_range(start, end):
            path = self.path_for(ticker, year)
            if not path.exists():
                continue
            df = pd.read_parquet(path)
            bars.extend(df.to_dict(orient="records"))

        if not bars:
            return []

        out: list[dict[str, Any]] = []
        for row in bars:
            d = row[DATE_COL]
            # Row dates may be pandas/pyarrow date objects or strings; normalise.
            if hasattr(d, "date"):
                d = d.date()
            elif isinstance(d, str):
                d = date.fromisoformat(d)
            if start <= d <= end:
                out.append(_normalise_row(row))
        return out

    def write_bars(
        self,
        ticker: str,
        bars: list[dict[str, Any]],
    ) -> None:
        """Replace/append the given bars into per-year parquet files.

        ``bars`` may span multiple years; we partition by year and merge
        into each year's file (dropping any prior rows for the same
        dates). The caller is responsible for ensuring only the right
        bars reach this method (e.g. no intraday bars from a live
        source).
        """
        if not bars:
            return

        rows = [_bar_to_row(b, ticker) for b in bars]
        df = pd.DataFrame(rows)

        for year, year_df in df.groupby(DATE_COL + "_year", sort=True):
            year_df = year_df.drop(columns=[DATE_COL + "_year"])
            self._write_year(ticker, int(year), year_df)

        log.info(
            "Wrote %d bars for %s into %d yearly file(s)",
            len(bars), ticker, df[DATE_COL + "_year"].nunique(),
        )

    def _write_year(self, ticker: str, year: int, new_df: pd.DataFrame) -> None:
        path = self.path_for(ticker, year)

        if path.exists():
            existing = pd.read_parquet(path)
            existing = existing[
                ~existing[DATE_COL].isin(new_df[DATE_COL].tolist())
            ]
            combined = pd.concat([existing, new_df], ignore_index=True)
        else:
            combined = new_df

        combined = combined.sort_values(DATE_COL).reset_index(drop=True)
        combined[DATE_COL] = pd.to_datetime(combined[DATE_COL]).dt.date.astype(
            "date32[pyarrow]"
        )
        combined[TIMESTAMP_COL] = combined[TIMESTAMP_COL].astype("int64")

        path.parent.mkdir(parents=True, exist_ok=True)
        write_parquet_atomic(combined, path)


def _bar_to_row(bar: dict[str, Any], ticker: str) -> dict[str, Any]:
    """Build a parquet row from upstream shape (t=ms, o/h/l/c, v).

    The parquet ``date`` column is the Nasdaq (US/Eastern) trading
    date of the bar; ``ts_ms`` is UTC midnight ms (Polygon's format).
    """
    ts_ms = int(bar["t"])
    bar_date = ny_from_ts(ts_ms)
    return {
        DATE_COL: bar_date,
        DATE_COL + "_year": bar_date.year,
        TICKER_COL: ticker.upper(),
        TIMESTAMP_COL: ts_ms,
        OPEN_COL: float(bar["o"]),
        HIGH_COL: float(bar["h"]),
        LOW_COL: float(bar["l"]),
        CLOSE_COL: float(bar["c"]),
        VOLUME_COL: float(bar.get("v", 0.0)),
        VWAP_COL: float(bar["vw"]) if bar.get("vw") is not None else None,
        TRADE_COUNT_COL: int(bar["n"]) if bar.get("n") is not None else None,
    }


def _normalise_row(row: dict[str, Any]) -> dict[str, Any]:
    """Strip parquet-only fields + coerce NaN to None."""
    out = dict(row)
    out.pop(DATE_COL + "_year", None)
    for k in NULLABLE_COLUMNS + [DATE_COL]:
        if k in out and out[k] is not None and _is_nan(out[k]):
            out[k] = None
    return out


def _is_nan(v: Any) -> bool:
    return isinstance(v, float) and math.isnan(v)