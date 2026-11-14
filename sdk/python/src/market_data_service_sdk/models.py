"""Typed responses for the market-data-service proxy.

These mirror the JSON shapes returned by the FastAPI app in
``app/api/constituents.py``, ``app/api/market_data.py`` and
``app/api/market_calendar.py``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date, datetime, timezone
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
    """One OHLCV bar for a ticker.

    A bar's identity is ``timestamp`` (epoch milliseconds at the bar's
    close time). Daily bars carry NY midnight; minute / hour bars carry
    the actual close time. There is no separate ``date`` field —
    callers who need a calendar day for a daily bar should derive it
    via ``datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)``
    or convert to US/Eastern explicitly.
    """

    ticker: str
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: Optional[float] = None
    trade_count: Optional[int] = None
    source: str = ""  # "cache" or "longbridge"

    @property
    def timestamp_utc(self) -> datetime:
        """Bar close time as a UTC ``datetime``."""
        return datetime.fromtimestamp(self.timestamp / 1000, tz=timezone.utc)


@dataclass
class BarsResponse:
    """OHLCV bars for a ticker across a closed date range."""

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


@dataclass
class MarketCalendarResponse:
    """The market session status of a single calendar date.

    Invariants relied on by callers:

    * ``is_trading_day`` is true exactly when ``status`` is ``regular``
      or ``early_close``.
    * ``is_early_close`` implies ``is_trading_day``.
    * ``is_holiday`` means "a weekday on which the exchange did not
      trade" — a weekend is *not* a holiday.
    * ``open`` / ``close`` are set exactly when the exchange held a
      session.
    """

    date: _date
    status: str
    is_trading_day: bool
    is_holiday: bool
    is_early_close: bool
    open: Optional[datetime] = None
    close: Optional[datetime] = None
