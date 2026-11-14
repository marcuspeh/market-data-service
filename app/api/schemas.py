"""Pydantic response models for the market-data-service API.

These ensure the wire format is consumable by all SDKs. Dates are
serialised as RFC3339 (``2006-01-02T15:04:05Z07:00``) so every SDK can
parse them with a single, unambiguous rule; the previous free-form
``dict`` response emitted bare ISO date strings (``"YYYY-MM-DD"``),
which Go could not parse.

Two kinds of date appear in these payloads, and they mean different
things:

* **Calendar-day markers** — ``BarOut.timestamp``, ``BarsResponseOut.from``
  / ``.to`` and ``MarketCalendarOut.date`` — identify a *day*, anchored
  at UTC midnight (``"2025-11-13T00:00:00Z"``). They are not instants:
  that string is the previous evening in US/Eastern.
* **Session instants** — ``MarketCalendarOut.open`` / ``.close`` — are
  real points on the wall clock and carry the US/Eastern offset
  (``"2025-11-13T09:30:00-05:00"``).

Timeframe note
-------------
A bar's identity is its ``timestamp`` (epoch milliseconds). Daily bars
serialise at NY midnight; minute / hour bars will carry the actual
bar close time. The deprecated ``date`` field has been removed to
avoid implying calendar-day semantics on intraday bars.
"""
from __future__ import annotations

from datetime import date as _date, datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


def _date_to_rfc3339(d: _date) -> str:
    """Serialize a calendar ``date`` as a UTC midnight RFC3339 timestamp.

    Matches the format Go's ``time.Time`` decodes natively, e.g.
    ``"2025-11-13T00:00:00Z"``.
    """
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _datetime_to_rfc3339(dt: datetime | None) -> str | None:
    """Serialize a timezone-aware ``datetime`` as RFC3339.

    Aware datetimes keep their UTC offset, so a US/Eastern session time
    reads ``"2025-11-13T09:30:00-05:00"``. ``None`` stays ``None`` —
    used for the session times of a day with no session.
    """
    if dt is None:
        return None
    return dt.isoformat()


class BarOut(BaseModel):
    ticker: str
    timestamp: int = Field(
        description=(
            "Bar close time in epoch milliseconds. "
            "Daily bars are at NY midnight; intraday bars at the actual close."
        )
    )
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float | None = None
    trade_count: int | None = None
    source: str = ""

    @classmethod
    def from_row(cls, row: dict) -> "BarOut":
        return cls(
            ticker=row["ticker"],
            timestamp=int(row["timestamp"]),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
            vwap=(float(row["vwap"]) if row.get("vwap") is not None else None),
            trade_count=(
                int(row["trade_count"]) if row.get("trade_count") is not None else None
            ),
            source=str(row.get("source", "")),
        )


class BarsResponseOut(BaseModel):
    ticker: str
    from_: str = Field(alias="from")
    to: str
    backfilled_bars: int
    bars: list[BarOut]

    model_config = {"populate_by_name": True}


def build_bars_response(payload: dict) -> BarsResponseOut:
    """Convert the service dict into the wire-shaped Pydantic model.

    ``payload["from"]`` / ``payload["to"]`` are Python ``date`` objects
    produced by :class:`MarketDataService`; we re-emit them as RFC3339
    strings for the same reason as ``BarOut.timestamp``.
    """
    return BarsResponseOut(
        ticker=payload["ticker"],
        **{"from": _date_to_rfc3339(payload["from"])},
        to=_date_to_rfc3339(payload["to"]),
        backfilled_bars=int(payload.get("backfilled_bars", 0)),
        bars=[BarOut.from_row(b) for b in payload.get("bars", [])],
    )


class MarketCalendarOut(BaseModel):
    """The market session status of a single calendar date.

    Invariants, relied on by every consumer:

    * ``is_trading_day`` is true exactly when ``status`` is ``regular``
      or ``early_close``.
    * ``is_early_close`` implies ``is_trading_day``.
    * ``is_holiday`` means "a weekday on which the exchange did not
      trade" — a weekend is *not* a holiday.
    * ``open`` / ``close`` are non-null exactly when the exchange held a
      session.
    """

    date: str = Field(
        description=(
            "The calendar date as a UTC-midnight RFC3339 marker, e.g. "
            '"2026-11-27T00:00:00Z". This identifies the day; it is not '
            "an instant (it is the previous evening in US/Eastern)."
        )
    )
    status: Literal["regular", "early_close", "holiday", "weekend"] = Field(
        description=(
            "Session status for the date. Describes the *session*, not the "
            "current moment: 'regular' is a full-length session, "
            "'early_close' a shortened one, 'holiday' a weekday with no "
            "session, 'weekend' a Saturday or Sunday."
        )
    )
    is_trading_day: bool = Field(
        description="True when the exchange held a session on this date."
    )
    is_holiday: bool = Field(
        description=(
            "True when this date is a market holiday — a weekday on which "
            "the exchange was closed. False for weekends."
        )
    )
    is_early_close: bool = Field(
        description="True when the session was shortened (e.g. a 1:00 PM close)."
    )
    open: str | None = Field(
        default=None,
        description=(
            "Session open time as RFC3339 with the US/Eastern offset, e.g. "
            '"2026-11-27T09:30:00-05:00". Null when there was no session.'
        ),
    )
    close: str | None = Field(
        default=None,
        description=(
            "Session close time as RFC3339 with the US/Eastern offset, e.g. "
            '"2026-11-27T13:00:00-05:00" on an early close. Null when there '
            "was no session."
        ),
    )


def build_market_calendar_response(payload: dict) -> MarketCalendarOut:
    """Convert the calendar service dict into the wire-shaped model.

    ``payload["date"]`` is a Python ``date``; the session times are
    timezone-aware ``datetime``s in US/Eastern.
    """
    return MarketCalendarOut(
        date=_date_to_rfc3339(payload["date"]),
        status=payload["status"],
        is_trading_day=bool(payload["is_trading_day"]),
        is_holiday=bool(payload["is_holiday"]),
        is_early_close=bool(payload["is_early_close"]),
        open=_datetime_to_rfc3339(payload.get("open")),
        close=_datetime_to_rfc3339(payload.get("close")),
    )
