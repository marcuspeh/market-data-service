"""Parquet-backed store for ETF constituents snapshots.

Layout (one parquet per ticker per calendar year):

    <constituents_dir>/SPY/2015.parquet
    <constituents_dir>/SPY/2024.parquet
    <constituents_dir>/QQQ/2015.parquet
    ...

Schema:

    date   : date32  (snapshot date)
    ticker : string  (the holding's ticker, e.g. "AAPL")

The ETF ticker itself is the directory name, not a column.

Reads for an arbitrary (start, end) range span one or more yearly
files. Writes are append-style: load the year's parquet (if any),
drop rows whose date matches the new snapshot, append the new rows,
write back atomically via a temp-file rename.
"""
import logging
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

DATE_COL = "date"
TICKER_COL = "ticker"
EMPTY_SCHEMA = [DATE_COL, TICKER_COL]


class ConstituentsNotFoundError(KeyError):
    def __init__(self, symbol: str, snapshot_date: date) -> None:
        self.symbol = symbol
        self.snapshot_date = snapshot_date
        super().__init__(f"No {symbol} constituents snapshot for {snapshot_date}")


class ConstituentsStore:
    def __init__(self, base_dir: str | Path):
        self._base_dir = Path(base_dir)

    def path_for(self, symbol: str, year: int) -> Path:
        return self._base_dir / symbol.upper() / f"{year}.parquet"

    def _years_for_range(self, start: date, end: date) -> list[int]:
        if end < start:
            return []
        return list(range(start.year, end.year + 1))

    def list_years(self, symbol: str) -> list[int]:
        ticker_dir = self._base_dir / symbol.upper()
        if not ticker_dir.exists():
            return []
        return sorted(
            int(p.stem)
            for p in ticker_dir.glob("*.parquet")
            if p.stem.isdigit()
        )

    def has_snapshot(self, symbol: str, snapshot_date: date) -> bool:
        path = self.path_for(symbol, snapshot_date.year)
        if not path.exists():
            return False
        df = pd.read_parquet(
            path, columns=[DATE_COL], filters=[(DATE_COL, "=", snapshot_date)]
        )
        return not df.empty

    def read_snapshot(self, symbol: str, snapshot_date: date) -> list[str]:
        path = self.path_for(symbol, snapshot_date.year)
        if not path.exists():
            raise ConstituentsNotFoundError(symbol, snapshot_date)

        df = pd.read_parquet(
            path,
            columns=[TICKER_COL],
            filters=[(DATE_COL, "=", snapshot_date)],
        )
        if df.empty:
            raise ConstituentsNotFoundError(symbol, snapshot_date)

        return [str(t) for t in df[TICKER_COL].tolist()]

    def read_range(
        self,
        symbol: str,
        start: date,
        end: date,
    ) -> dict[date, list[str]]:
        """Return ``{snapshot_date: [holding_tickers]}`` for any snapshots
        found in the (start, end) window. Years with no file contribute
        nothing."""
        out: dict[date, list[str]] = {}
        for year in self._years_for_range(start, end):
            path = self.path_for(symbol, year)
            if not path.exists():
                continue
            df = pd.read_parquet(
                path,
                columns=[DATE_COL, TICKER_COL],
                filters=[(DATE_COL, ">=", start), (DATE_COL, "<=", end)],
            )
            if df.empty:
                continue
            for snap_date, group in df.groupby(DATE_COL, sort=True):
                d = snap_date.date() if hasattr(snap_date, "date") else snap_date
                out[d] = [str(t) for t in group[TICKER_COL].tolist()]
        return out

    def list_snapshot_dates(self, symbol: str) -> list[date]:
        ticker_dir = self._base_dir / symbol.upper()
        if not ticker_dir.exists():
            return []
        dates: set[date] = set()
        for p in sorted(ticker_dir.glob("*.parquet")):
            if not p.stem.isdigit():
                continue
            df = pd.read_parquet(p, columns=[DATE_COL])
            for value in df[DATE_COL]:
                d = value.date() if hasattr(value, "date") else value
                dates.add(d)
        return sorted(dates)

    def write_snapshot(
        self,
        symbol: str,
        snapshot_date: date,
        tickers: list[str],
    ) -> None:
        """Append (or replace) a snapshot row-group into the ticker's
        ``<year>.parquet`` file."""
        year = snapshot_date.year
        path = self.path_for(symbol, year)
        new_rows = pd.DataFrame(
            {
                DATE_COL: [snapshot_date] * len(tickers),
                TICKER_COL: tickers,
            }
        )

        if path.exists():
            existing = pd.read_parquet(path)
            existing = existing[existing[DATE_COL] != snapshot_date]
            combined = pd.concat([existing, new_rows], ignore_index=True)
        else:
            combined = new_rows

        combined[DATE_COL] = pd.to_datetime(combined[DATE_COL]).dt.date.astype(
            "date32[pyarrow]"
        )

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        combined.to_parquet(tmp, index=False, engine="pyarrow")
        tmp.replace(path)  # atomic on POSIX

        logger.info(
            f"Wrote {len(tickers)} rows for {symbol} on {snapshot_date} -> {path}"
        )