"""Parquet-backed store for per-ticker constituents snapshots.

Layout:
    <constituents_dir>/SPY.parquet
    <constituents_dir>/QQQ.parquet
    <constituents_dir>/IWM.parquet

Each parquet file holds the cumulative history of snapshots for one ETF
ticker. Schema:

    date   : date32  (snapshot date)
    ticker : string  (the holding's ticker, e.g. "AAPL")

The ETF ticker itself is the filename, not a column.

Reads filter to the requested snapshot date and return a flat list of
holding tickers. Writes are append-style: load the existing parquet (if
any), drop any prior rows for the snapshot date, concatenate the new
rows, write back atomically via a temp-file rename.
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
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, symbol: str) -> Path:
        return self._base_dir / f"{symbol.upper()}.parquet"

    def read_snapshot(self, symbol: str, snapshot_date: date) -> list[str]:
        path = self.path_for(symbol)
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

    def has_snapshot(self, symbol: str, snapshot_date: date) -> bool:
        path = self.path_for(symbol)
        if not path.exists():
            return False
        df = pd.read_parquet(
            path, columns=[DATE_COL], filters=[(DATE_COL, "=", snapshot_date)]
        )
        return not df.empty

    def list_snapshot_dates(self, symbol: str) -> list[date]:
        path = self.path_for(symbol)
        if not path.exists():
            return []
        df = pd.read_parquet(path, columns=[DATE_COL])
        return sorted({d for d in df[DATE_COL].tolist()})

    def write_snapshot(
        self,
        symbol: str,
        snapshot_date: date,
        tickers: list[str],
    ) -> None:
        path = self.path_for(symbol)
        new_rows = pd.DataFrame(
            {
                DATE_COL: [snapshot_date] * len(tickers),
                TICKER_COL: tickers,
            }
        )

        existing = (
            pd.read_parquet(path)
            if path.exists()
            else pd.DataFrame(columns=EMPTY_SCHEMA)
        )

        existing = existing[existing[DATE_COL] != snapshot_date]
        combined = pd.concat([existing, new_rows], ignore_index=True)

        combined[DATE_COL] = pd.to_datetime(combined[DATE_COL]).dt.date.astype(
            "date32[pyarrow]"
        )

        tmp = path.with_suffix(path.suffix + ".tmp")
        combined.to_parquet(tmp, index=False, engine="pyarrow")
        tmp.replace(path)  # atomic on POSIX

        logger.info(
            f"Wrote {len(tickers)} rows for {symbol} on {snapshot_date} -> {path}"
        )