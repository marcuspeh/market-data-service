"""Typed responses for the market-data-service proxy.

These mirror the JSON shapes returned by the FastAPI app in
``app/api/constituents.py`` and ``app/api/market_data.py``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date
from typing import Optional


@dataclass
class ConstituentsResponse:
    """Snapshot of an ETF's holdings for a specific calendar date."""

    symbol: str
    date: _date
    constituents: list[str]
    source: str


@dataclass
class Bar:
    """One daily OHLCV bar for a ticker."""

    ticker: str
    date: _date
    timestamp: int  # epoch milliseconds (Nasdaq calendar day, NY tz)
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: Optional[float] = None
    trade_count: Optional[int] = None
    source: str = ""  # "cache" or "longbridge"


@dataclass
class BarsResponse:
    """Daily OHLCV bars for a ticker across a closed date range."""

    ticker: str
    from_: _date
    to: _date
    backfilled_bars: int
    bars: list[Bar]

    # Field aliases for round-tripping the API's JSON without renaming.
    # ``from`` is a Python keyword, so the wire name is exposed via the
    # ``from_`` constructor argument and the ``from`` property below.
    @property
    def from_date(self) -> _date:
        return self.from_

    @property
    def to_date(self) -> _date:
        return self.to
