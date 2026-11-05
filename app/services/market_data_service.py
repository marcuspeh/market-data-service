"""Daily OHLCV bar orchestrator: raw parquet cache + Polygon backfill + Longbridge live bar.

Bars are persisted exactly as the upstreams publish them. Splits and
cash dividends live in a separate store
(:mod:`app.services.corporate_actions_store`) and the adjusted view is
rebuilt on every read by :func:`app.services.adjustments.apply_adjustments`.
Stored prices therefore never move, and reconciliation is a cheap
corporate-action sync instead of a price rewrite.

Today's bar comes from Longbridge, is adjusted through the same read
path, and is never persisted — Polygon owns settled history.

Adjustment basis
----------------
The returned series is anchored at the last bar of the requested window:
an action dated after ``to`` does not rescale the response. Within the
window itself the series is deterministic — the same raw bars and the
same action records always produce the same numbers.
"""
import time
from datetime import date, timedelta
from typing import Any

from app.clients.longbridge import LongbridgeClient, LongbridgeError
from app.clients.polygon import PolygonClient, PolygonError
from app.config.settings import Settings, ny_from_ts
from app.logging_setup import client
from app.services.adjustments import (
    apply_adjustments,
    dividend_action,
    split_action,
)
from app.services.corporate_actions_store import CorporateActionsStore
from app.services.market_bars_store import MarketBarsStore

log = client()

#: Extra calendar days read before ``start`` so a dividend whose ex-date
#: opens the requested window still has a prior close to be sized against.
LOOKBACK = timedelta(days=10)

#: Trailing window the daily backfill refreshes corporate actions over.
ACTION_REFRESH_DAYS = 30

#: Widest run of consecutive non-trading days in the US market calendar.
#: The longest closure is the Christmas/New Year cluster (Dec 24-26 plus a
#: weekend = 4 non-trading days), so two consecutive bars are never more
#: than 5 calendar days apart. A wider span means real bars are missing
#: and the range has to be refetched.
MAX_CLOSURE_SPAN_DAYS = 5

#: How long a corporate-action sync counts as fresh for a ticker. Actions
#: are re-synced on every read because a split or dividend can be declared
#: at any time; the TTL keeps a burst of requests from turning into a burst
#: of upstream calls.
ACTION_SYNC_TTL_SECONDS = 300.0


class MarketDataService:
    def __init__(
        self,
        settings: Settings,
        store: MarketBarsStore | None = None,
        actions_store: CorporateActionsStore | None = None,
        polygon: PolygonClient | None = None,
        longbridge: LongbridgeClient | None = None,
    ) -> None:
        self._settings = settings
        self._store = store or MarketBarsStore(settings.market_data_dir)
        self._actions = actions_store or CorporateActionsStore(
            settings.corporate_actions_dir
        )
        self._polygon = polygon or PolygonClient(settings)
        self._longbridge = longbridge or LongbridgeClient(settings)
        self._store.ensure_raw_basis()
        #: Monotonic timestamp of the last corporate-action sync per ticker.
        self._action_synced_at: dict[str, float] = {}
        #: The window each ticker's last action sync covered.
        self._action_synced_window: dict[str, tuple[date, date]] = {}

    async def get_bars(
        self,
        ticker: str,
        start: date,
        end: date,
    ) -> dict[str, Any]:
        ticker = ticker.upper()

        if end < start:
            raise ValueError("'to' must be on or after 'from'")

        # The live trading day is the Nasdaq calendar day, not UTC.
        today = Settings.now_ny_date()
        historical_end = min(end, today - timedelta(days=1))
        # Actions dated after ``end`` are irrelevant: the series is
        # anchored at the window's last bar.
        action_end = min(end, today)

        rows: list[dict[str, Any]] = []
        backfilled_bars = 0

        if start <= historical_end:
            cache_start = start - LOOKBACK
            extent = self._store.cached_extent(ticker, before=today)
            backfill = self._backfill_window(cache_start, historical_end, extent)

            if backfill is None and self._has_interior_gap(
                ticker, cache_start, historical_end, extent
            ):
                # The cache holds disjoint spans — refetch the whole range
                # so the hole is filled rather than served as an empty
                # stretch of the response.
                backfill = (cache_start, historical_end)

            if backfill is not None:
                backfilled_bars = await self._backfill_bars(
                    ticker, backfill[0], backfill[1], today
                )

            # Corporate actions are refreshed independently of the bar
            # backfill: a split or dividend can be declared at any time, so
            # a warm cache must still pick it up. The TTL stops a burst of
            # requests from turning into a burst of upstream calls.
            await self._sync_actions(ticker, cache_start, action_end)

            cached = self._store.read_range(ticker, cache_start, historical_end)
            rows.extend(
                {**row, "source": "cache"}
                for row in cached
                if row["date"] < today
            )

        if start <= today <= end:
            log.info("Fetching today's daily bar for %s from Longbridge", ticker)
            try:
                today_bar = await self._longbridge.fetch_today_bar(ticker)
            except LongbridgeError as e:
                log.error("Longbridge fetch failed for %s: %s", ticker, e)
                raise

            if today_bar is not None:
                live_row = self._normalize_today_bar(today_bar, ticker)
                if live_row["date"] != today:
                    # The upstream's latest completed session isn't today's
                    # (before the open, or on a weekend/holiday). That bar
                    # is already in the cache, so serving it again would
                    # duplicate the date and leave today missing.
                    log.info(
                        "Ignoring Longbridge bar for %s dated %s; "
                        "today is %s",
                        ticker, live_row["date"], today,
                    )
                elif any(row["date"] == today for row in rows):
                    log.info(
                        "Ignoring Longbridge bar for %s; %s is already cached",
                        ticker, today,
                    )
                else:
                    rows.append({**live_row, "source": "longbridge"})
            else:
                log.info("Longbridge returned no bar for %s today", ticker)

        actions = self._actions.read(ticker, start - LOOKBACK, action_end)
        adjusted = apply_adjustments(rows, actions)
        # ``rows`` may include the pre-window lookback used to size a
        # dividend that opens the window; only the window is returned.
        bars = [row for row in adjusted if start <= row["date"] <= end]
        bars.sort(key=lambda b: b["timestamp"])

        return {
            "ticker": ticker,
            "from": start,
            "to": end,
            "backfilled_bars": backfilled_bars,
            "bars": bars,
        }

    async def backfill_yesterday(self, ticker: str) -> int:
        """Fetch yesterday's daily bar from Polygon and persist it raw.

        Corporate actions are refreshed over a trailing window on the
        same pass so a newly declared split or dividend shows up in the
        next read without waiting for the weekly reconciliation.
        """
        ticker = ticker.upper()
        today = Settings.now_ny_date()
        yesterday = today - timedelta(days=1)
        try:
            bars = await self._polygon.fetch_daily_bars(
                ticker, yesterday, yesterday
            )
        except PolygonError as e:
            log.error("Polygon fetch for %s yesterday failed: %s", ticker, e)
            return 0
        if not bars:
            log.info("Polygon returned no bar for %s on %s", ticker, yesterday)
            return 0

        self._store.write_bars(ticker, bars)
        try:
            await self._sync_actions(
                ticker, today - timedelta(days=ACTION_REFRESH_DAYS), today
            )
        except PolygonError as e:
            log.error("Corporate action refresh failed for %s: %s", ticker, e)

        log.info("Cached %s %s from Polygon", ticker, yesterday)
        return 1

    async def reconcile_ticker(self, ticker: str) -> dict[str, Any]:
        """Bring a ticker's corporate actions current and extend its cache.

        Raw prices never need re-basing, so reconciliation only has to
        (a) merge the split/dividend records Polygon publishes for the
        cached window and (b) fill the trading days between the newest
        cached bar and yesterday. Both steps only ever add or replace
        individual records, so a partial upstream response can never
        destroy cached history.
        """
        ticker = ticker.upper()
        today = Settings.now_ny_date()
        historical_end = today - timedelta(days=1)

        extent = self._store.cached_extent(ticker, before=today)
        if extent is None:
            log.info("No cached bars for %s; nothing to reconcile", ticker)
            return {
                "ticker": ticker,
                "status": "no_cached_data",
                "actions_fetched": 0,
                "actions_stored": 0,
                "bars_written": 0,
            }

        first, last = extent
        log.info("Reconciling %s: cached %s..%s", ticker, first, last)

        try:
            fetched = await self._fetch_actions(ticker, first, historical_end)
        except PolygonError as e:
            log.error("Polygon action fetch for %s failed: %s", ticker, e)
            return {
                "ticker": ticker,
                "status": "error",
                "actions_fetched": 0,
                "actions_stored": 0,
                "bars_written": 0,
                "error": str(e),
            }

        actions_stored = self._actions.upsert(ticker, fetched)

        bars_written = 0
        if last < historical_end or self._has_interior_gap(
            ticker, first, historical_end, extent
        ):
            try:
                if last < historical_end:
                    bars_written = await self._backfill_bars(
                        ticker, last + timedelta(days=1), historical_end, today
                    )
                else:
                    # The cache has disjoint spans; refetch the whole
                    # covered window so the interior hole is filled.
                    bars_written = await self._backfill_bars(
                        ticker, first, historical_end, today
                    )
            except PolygonError as e:
                log.error("Polygon bar refresh for %s failed: %s", ticker, e)
                return {
                    "ticker": ticker,
                    "status": "error",
                    "actions_fetched": len(fetched),
                    "actions_stored": actions_stored,
                    "bars_written": 0,
                    "error": str(e),
                }

        return {
            "ticker": ticker,
            "status": "synced",
            "actions_fetched": len(fetched),
            "actions_stored": actions_stored,
            "bars_written": bars_written,
        }

    async def reconcile_all(self) -> dict[str, dict[str, Any]]:
        """Reconcile every ticker that has cached bars."""
        results: dict[str, dict[str, Any]] = {}
        for ticker in self._store.list_tickers():
            try:
                results[ticker] = await self.reconcile_ticker(ticker)
            except Exception as e:  # noqa: BLE001 — one bad ticker shouldn't stop the rest
                log.error("Reconciliation failed for %s: %s", ticker, e)
                results[ticker] = {
                    "ticker": ticker,
                    "status": "error",
                    "actions_fetched": 0,
                    "actions_stored": 0,
                    "bars_written": 0,
                    "error": str(e),
                }
        return results

    async def _backfill_bars(
        self, ticker: str, start: date, end: date, today: date
    ) -> int:
        """Fetch raw bars from Polygon and persist the settled ones."""
        log.info("Backfilling %s from Polygon (%s..%s)", ticker, start, end)
        try:
            polygon_bars = await self._polygon.fetch_daily_bars(
                ticker, start, end
            )
        except PolygonError as e:
            log.error("Polygon fetch failed for %s: %s", ticker, e)
            raise

        # Polygon can return today's intraday bar; only settled days are
        # persisted (today's bar is served live from Longbridge).
        persistable = [b for b in polygon_bars if self._bar_date_ny(b) < today]
        if persistable:
            self._store.write_bars(ticker, persistable)
        return len(persistable)

    async def _fetch_actions(
        self, ticker: str, start: date, end: date
    ) -> list[dict[str, Any]]:
        """Fetch + normalise splits and cash dividends for ``start..end``."""
        if end < start:
            return []

        splits = await self._polygon.fetch_splits(ticker, start, end)
        dividends = await self._polygon.fetch_dividends(ticker, start, end)

        actions: list[dict[str, Any]] = []
        for payload in splits:
            action = split_action(payload)
            if action is not None:
                actions.append(action)
        for payload in dividends:
            action = dividend_action(payload)
            if action is not None:
                actions.append(action)
        return actions

    async def _sync_actions(self, ticker: str, start: date, end: date) -> int:
        """Refresh and persist corporate actions for ``start..end``.

        Skipped when a sync covering the same window ran within
        :data:`ACTION_SYNC_TTL_SECONDS`, so a burst of reads doesn't turn
        into a burst of upstream calls. A request for a *wider* window
        always syncs — the cached window wouldn't cover it.
        """
        if end < start:
            return 0

        now = time.monotonic()
        last = self._action_synced_at.get(ticker)
        if (
            last is not None
            and now - last < ACTION_SYNC_TTL_SECONDS
            and self._action_synced_window.get(ticker) == (start, end)
        ):
            return 0

        actions = await self._fetch_actions(ticker, start, end)
        stored = self._actions.upsert(ticker, actions)
        self._action_synced_at[ticker] = now
        self._action_synced_window[ticker] = (start, end)
        return stored

    @staticmethod
    def _backfill_window(
        start: date, end: date, extent: tuple[date, date] | None
    ) -> tuple[date, date] | None:
        """Smallest Polygon range that fills the cache's uncovered edges.

        Interior holes are deliberately left alone: ``write_bars`` always
        persists a whole fetched range, so a gap inside the cached extent
        is a non-trading day (weekend / holiday), not a missing bar.
        """
        if extent is None:
            return (start, end)

        first, last = extent
        if start < first and last < end:
            # The cache is an interior slice of the request.
            return (start, end)
        if start < first:
            return (start, min(end, first - timedelta(days=1)))
        if last < end:
            return (max(start, last + timedelta(days=1)), end)
        return None

    def _has_interior_gap(
        self, ticker: str, start: date, end: date, extent: tuple[date, date] | None
    ) -> bool:
        """True when the cached bars don't continuously cover ``start..end``.

        A genuine market closure never spans more than
        :data:`MAX_CLOSURE_SPAN_DAYS` calendar days, so a wider gap
        between two consecutive cached bars means bars are missing — the
        cache holds two disjoint spans rather than one history.
        """
        if extent is None:
            return False

        cached = self._store.cached_dates(ticker, start, end)
        if len(cached) < 2:
            # Fewer than two bars can't have an interior gap.
            return False

        for previous, current in zip(cached, cached[1:]):
            if (current - previous).days > MAX_CLOSURE_SPAN_DAYS:
                return True
        return False

    @staticmethod
    def _bar_date_ny(bar: dict[str, Any]) -> date:
        """Nasdaq calendar date for a Polygon-shaped bar."""
        return ny_from_ts(int(bar["t"]))

    @staticmethod
    def _normalize_today_bar(bar: dict[str, Any], ticker: str) -> dict[str, Any]:
        """Reshape a Longbridge bar dict to match cache / Polygon rows."""
        ts_ms = int(bar["t"])
        bar_date = ny_from_ts(ts_ms)
        return {
            "ticker": ticker,
            "date": bar_date,
            "timestamp": ts_ms,
            "open": float(bar["o"]),
            "high": float(bar["h"]),
            "low": float(bar["l"]),
            "close": float(bar["c"]),
            "volume": float(bar.get("v", 0.0)),
            "vwap": float(bar["vw"]) if bar.get("vw") is not None else None,
            "trade_count": int(bar["n"]) if bar.get("n") is not None else None,
        }
