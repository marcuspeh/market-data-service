"""Parquet-backed store for per-ticker corporate actions.

Layout (one file per ticker):

    <corporate_actions_dir>/AAPL.parquet
    <corporate_actions_dir>/MSFT.parquet

Schema:

    date        : date32   (execution date for splits, ex-date for dividends)
    kind        : string   ("split" | "dividend")
    split_from  : double (nullable)
    split_to    : double (nullable)
    cash_amount : double (nullable)

The records are the inputs to the read-time back-adjustment in
:mod:`app.services.adjustments`, which is why they are persisted apart
from the raw price bars: they change rarely, reconcile cheaply, and can
be re-fetched without touching stored prices.
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from app.logging_setup import client
from app.services.parquet_io import write_parquet_atomic

log = client()

DATE_COL = "date"
KIND_COL = "kind"
SPLIT_FROM_COL = "split_from"
SPLIT_TO_COL = "split_to"
CASH_COL = "cash_amount"

COLUMNS = [DATE_COL, KIND_COL, SPLIT_FROM_COL, SPLIT_TO_COL, CASH_COL]


class CorporateActionsStore:
    def __init__(self, base_dir: str | Path):
        self._base_dir = Path(base_dir)

    def path_for(self, ticker: str) -> Path:
        return self._base_dir / f"{ticker.upper()}.parquet"

    def read(
        self, ticker: str, start: date, end: date
    ) -> list[dict[str, Any]]:
        """Actions with ``start <= date <= end``, sorted by (date, kind)."""
        path = self.path_for(ticker)
        if not path.exists():
            return []

        df = pd.read_parquet(path)
        if df.empty:
            return []

        actions: list[dict[str, Any]] = []
        for row in df.to_dict(orient="records"):
            d = _as_date(row.get(DATE_COL))
            if d is None or d < start or d > end:
                continue
            actions.append(_normalise_row(row, d))

        actions.sort(key=lambda a: (a[DATE_COL], a[KIND_COL]))
        return actions

    def upsert(
        self,
        ticker: str,
        actions: list[dict[str, Any]],
    ) -> int:
        """Merge ``actions`` into the ticker's file.

        Records are keyed by ``(date, kind)``: a re-fetched action
        replaces its stored copy, but nothing is ever dropped. Corporate
        actions are immutable historical facts, so a response that omits
        an older record (e.g. Polygon's free tier only returns two years
        of dividends) must not erase history that is already known.

        Returns the number of distinct records on disk after the merge.
        """
        if not actions:
            return 0

        path = self.path_for(ticker)

        merged: dict[tuple[date, str], dict[str, Any]] = {}
        if path.exists():
            df = pd.read_parquet(path)
            for row in df.to_dict(orient="records"):
                d = _as_date(row.get(DATE_COL))
                if d is None:
                    continue
                normalised = _normalise_row(row, d)
                merged[(d, normalised[KIND_COL])] = normalised

        for action in actions:
            d = _as_date(action.get(DATE_COL))
            if d is None:
                continue
            normalised = _normalise_row(action, d)
            merged[(d, normalised[KIND_COL])] = normalised

        rows = sorted(
            merged.values(), key=lambda a: (a[DATE_COL], a[KIND_COL])
        )
        out = pd.DataFrame(rows, columns=COLUMNS)
        out[DATE_COL] = pd.to_datetime(out[DATE_COL]).dt.date.astype(
            "date32[pyarrow]"
        )

        path.parent.mkdir(parents=True, exist_ok=True)
        write_parquet_atomic(out, path)

        log.info(
            "Stored %d corporate action record(s) for %s",
            len(rows), ticker.upper(),
        )
        return len(rows)


def _normalise_row(row: dict[str, Any], d: date) -> dict[str, Any]:
    def _float(key: str) -> float | None:
        value = row.get(key)
        if value is None or pd.isna(value):
            return None
        return float(value)

    return {
        DATE_COL: d,
        KIND_COL: str(row[KIND_COL]),
        SPLIT_FROM_COL: _float(SPLIT_FROM_COL),
        SPLIT_TO_COL: _float(SPLIT_TO_COL),
        CASH_COL: _float(CASH_COL),
    }


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None
