"""Pydantic response models for the market-data-service API.

These ensure the wire format is consumable by all SDKs. In particular,
the Go SDK's ``time.Time`` JSON unmarshaler requires RFC3339
(``2006-01-02T15:04:05Z07:00``); the previous free-form ``dict``
response emitted bare ISO date strings (``"YYYY-MM-DD"``), which Go
could not parse.

Timeframe note
-------------
A bar's identity is its ``timestamp`` (epoch milliseconds). Daily bars
serialise at NY midnight; minute / hour bars will carry the actual
bar close time. The deprecated ``date`` field has been removed to
avoid implying calendar-day semantics on intraday bars.
"""
from __future__ import annotations

from datetime import date as _date, datetime, timezone

from pydantic import BaseModel, Field


def _date_to_rfc3339(d: _date) -> str:
    """Serialize a calendar ``date`` as a UTC midnight RFC3339 timestamp.

    Matches the format Go's ``time.Time`` decodes natively, e.g.
    ``"2025-11-13T00:00:00Z"``.
    """
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


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