"""Back-adjustment of raw daily bars for splits and cash dividends.

The parquet cache stores prices exactly as Polygon published them
(``adjusted=false``). The adjusted series is rebuilt on every read from
that immutable raw series plus the ticker's corporate-action records, so
stored numbers never move and reconciliations stay deterministic.

Back-adjustment anchors the series to the latest bar: every bar strictly
*before* an action's date is scaled by that action's factor.

    split     ``split_from / split_to`` for prices, the inverse for volume
    dividend  ``1 - cash_amount / previous_close`` for prices only

A dividend's factor is sized off the previous trading day's close in
*today's* basis (split-adjusted, and already scaled by any later
dividends), mirroring the CRSP convention. ``cash_amount`` is therefore
taken from Polygon's ``split_adjusted_cash_amount`` when available, so
cash and price are expressed in the same share basis.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from datetime import date
from typing import Any

from app.logging_setup import client

log = client()

SPLIT = "split"
DIVIDEND = "dividend"

PRICE_COLUMNS = ("open", "high", "low", "close")


def split_action(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Normalise a Polygon ``/stocks/v1/splits`` row; ``None`` if unusable."""
    try:
        d = date.fromisoformat(str(payload["execution_date"]))
        split_from = float(payload["split_from"])
        split_to = float(payload["split_to"])
    except (KeyError, TypeError, ValueError):
        log.warn("Skipping malformed split payload: %s", payload)
        return None
    if split_from <= 0 or split_to <= 0:
        log.warn("Skipping split with non-positive ratio: %s", payload)
        return None
    return {
        "date": d,
        "kind": SPLIT,
        "split_from": split_from,
        "split_to": split_to,
        "cash_amount": None,
    }


def dividend_action(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Normalise a Polygon ``/stocks/v1/dividends`` row; ``None`` if unusable."""
    # Only cash dividends move the price series; stock dividends already
    # show up as splits and capital-gain distributions are not dividends.
    distribution = payload.get("distribution_type")
    if distribution not in (None, "CD"):
        return None

    cash = payload.get("split_adjusted_cash_amount")
    if cash is None:
        cash = payload.get("cash_amount")
    try:
        d = date.fromisoformat(str(payload["ex_dividend_date"]))
        amount = float(cash)
    except (KeyError, TypeError, ValueError):
        log.warn("Skipping malformed dividend payload: %s", payload)
        return None
    if amount <= 0:
        return None
    return {
        "date": d,
        "kind": DIVIDEND,
        "split_from": None,
        "split_to": None,
        "cash_amount": amount,
    }


def apply_adjustments(
    rows: list[dict[str, Any]],
    actions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return ``rows`` split/dividend back-adjusted, sorted by date.

    ``rows`` are cache/Longbridge shaped (``date``, ``open``/``high``/
    ``low``/``close``, ``volume``, optional ``vwap``). Input rows are not
    mutated.
    """
    if not rows or not actions:
        return rows

    rows = sorted(rows, key=lambda r: r["date"])
    bar_dates = [r["date"] for r in rows]

    split_multipliers = _multipliers(
        (a["date"], a["split_from"] / a["split_to"])
        for a in actions
        if a["kind"] == SPLIT
    )
    split_factors = [_factor_at(split_multipliers, d) for d in bar_dates]

    # Dividend drops are measured against the prior close in today's
    # split basis, so size them off the split-adjusted closes.
    split_adjusted_closes = [
        float(row["close"]) * factor for row, factor in zip(rows, split_factors)
    ]
    dividend_multipliers = _multipliers(
        _dividend_drops(bar_dates, split_adjusted_closes, actions)
    )

    out: list[dict[str, Any]] = []
    for row, bar_date, split_factor in zip(rows, bar_dates, split_factors):
        price_factor = split_factor * _factor_at(dividend_multipliers, bar_date)
        volume_factor = 1.0 / split_factor

        adjusted = dict(row)
        for column in PRICE_COLUMNS:
            adjusted[column] = float(row[column]) * price_factor
        if row.get("vwap") is not None:
            adjusted["vwap"] = float(row["vwap"]) * price_factor
        adjusted["volume"] = float(row["volume"]) * volume_factor
        out.append(adjusted)
    return out


def _dividend_drops(
    bar_dates: list[date],
    split_adjusted_closes: list[float],
    actions: list[dict[str, Any]],
) -> list[tuple[date, float]]:
    """``(ex_date, price_multiplier)`` for each usable cash dividend.

    Later dividends are processed first so ``cumulative`` carries the
    product of every dividend that post-dates the one being sized.
    """
    dividends = sorted(
        (a for a in actions if a["kind"] == DIVIDEND), key=lambda a: a["date"]
    )

    drops: list[tuple[date, float]] = []
    cumulative = 1.0
    for dividend in reversed(dividends):
        index = bisect_left(bar_dates, dividend["date"]) - 1
        if index < 0:
            # No prior bar to size the drop against (ex-date precedes the
            # cached history); the bar series is already in the right basis.
            continue
        previous_close = split_adjusted_closes[index] * cumulative
        if previous_close <= 0:
            continue
        multiplier = 1.0 - float(dividend["cash_amount"]) / previous_close
        if multiplier <= 0:
            log.warn(
                "Ignoring dividend of %s on %s: it exceeds the previous close",
                dividend["cash_amount"], dividend["date"],
            )
            continue
        drops.append((dividend["date"], multiplier))
        cumulative *= multiplier
    return drops


def _multipliers(
    events: Any,
) -> tuple[list[date], list[float]]:
    """Suffix products for ``(date, multiplier)`` events.

    Returns ``(dates_ascending, suffix)`` where ``suffix[i]`` is the
    product of every multiplier at index ``i`` or later.
    """
    ordered = sorted(events, key=lambda e: e[0])
    if not ordered:
        return [], []

    dates = [e[0] for e in ordered]
    suffix = [1.0] * (len(ordered) + 1)
    for i in range(len(ordered) - 1, -1, -1):
        suffix[i] = suffix[i + 1] * ordered[i][1]
    return dates, suffix


def _factor_at(multipliers: tuple[list[date], list[float]], bar_date: date) -> float:
    """Product of every action strictly *after* ``bar_date``."""
    dates, suffix = multipliers
    if not dates:
        return 1.0
    return suffix[bisect_right(dates, bar_date)]
