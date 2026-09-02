"""Parquet-backed store for daily OHLCV bars.

Layout (one parquet per ticker per calendar year):

    <data_dir>/market/AAPL/2024.parquet
    <data_dir>/market/AAPL/2025.parquet
    <data_dir>/market/MSFT/2024.parquet
    ...

Schema:

    date        : date32   (bar date)
    ticker      : string
    timestamp   : int64    (UTC midnight in ms)
    open/high/low/close : double
    volume      : double
    vwap        : double (nullable)
    trade_count : int64  (nullable)

Rows are always sorted ascending by ``date`` inside each file.

Reads for an arbitrary (start, end) range span one or more yearly files.
Writes are append-style: load the year's parquet (if any), drop rows
whose date matches the new bars, concatenate the new bars, sort by
date ascending, and write back atomically via a temp-file rename.
"""
import logging
import math
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from app.config.settings import ny_from_ts, ny_midnight_ts

logger = logging.getLogger(__name__)

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


def _date_to_ts(d: date) -> int:
    """NY-midnight epoch-ms for ``d``."""
    return ny_midnight_ts(d)


class MarketBarNotFoundError(KeyError):
    def __init__(self, ticker: str, bar_date: date) -> None:
        self.ticker = ticker
        self.bar_date = bar_date
        super().__init__(f"No cached bar for {ticker} on {bar_date}")


class MarketBarsStore:
    def __init__(self, base_dir: str | Path):
        self._base_dir = Path(base_dir)

    def path_for(self, ticker: str, year: int) -> Path:
        return self._base_dir / ticker.upper() / f"{year}.parquet"

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

    def existing_dates(
        self,
        ticker: str,
        start: date,
        end: date,
    ) -> set[date]:
        return {
            row["bar_date"]
            for row in self.read_range(ticker, start, end)
            for k in ["bar_date"]
            if k in row
        }

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

        logger.info(
            f"Wrote {len(bars)} bars for {ticker} "
            f"into {df[DATE_COL + '_year'].nunique()} yearly file(s)"
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
        tmp = path.with_suffix(path.suffix + ".tmp")
        combined.to_parquet(tmp, index=False, engine="pyarrow")
        tmp.replace(path)


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