"""Cache-and-proxy orchestrator for **daily** OHLCV bars.

Strategy:
    * Historical bars (``start..today-1``) are served from the local
      parquet cache, backfilled from Polygon on miss, and persisted into
      per-year files under ``<data_dir>/market/<TICKER>/<YEAR>.parquet``.
    * Today's bar is sourced live from Interactive Brokers via
      ``IBKRClient`` and **never** persisted — the current-day bar may
      still be forming. It is cached in-process with a 5-minute TTL
      so repeated requests within the window don't hit IBKR.
"""
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.clients.ibkr import IBKRClient, IBKRError
from app.clients.polygon import PolygonClient, PolygonError
from app.config.settings import Settings
from app.services.market_bars_store import MarketBarsStore

logger = logging.getLogger(__name__)


class MarketDataService:
    def __init__(
        self,
        settings: Settings,
        store: MarketBarsStore | None = None,
        polygon: PolygonClient | None = None,
        ibkr: IBKRClient | None = None,
    ) -> None:
        self._settings = settings
        self._store = store or MarketBarsStore(settings.market_data_dir)
        self._polygon = polygon or PolygonClient(settings)
        self._ibkr = ibkr or IBKRClient(settings)

    # ------------------------------------------------------------------ read

    async def get_bars(
        self,
        ticker: str,
        start: date,
        end: date,
    ) -> dict[str, Any]:
        ticker = ticker.upper()

        if end < start:
            raise ValueError("'to' must be on or after 'from'")

        today = datetime.now(timezone.utc).date()
        historical_end = min(end, today - timedelta(days=1))

        bars: list[dict[str, Any]] = []
        backfilled_bars = 0

        # ----- Historical portion: parquet cache + Polygon backfill ---------
        if start <= historical_end:
            cached = self._store.read_range(ticker, start, historical_end)
            cached_dates = {row["date"] for row in cached}
            missing = self._missing_dates(
                start, historical_end, cached_dates
            )

            if missing:
                # Use the smallest window that covers the missing dates.
                backfill_start = missing[0]
                backfill_end = missing[-1]
                logger.info(
                    f"{len(missing)} missing date(s) for {ticker}; "
                    f"backfilling from Polygon "
                    f"({backfill_start}..{backfill_end})"
                )
                try:
                    polygon_bars = await self._polygon.fetch_daily_bars(
                        ticker, backfill_start, backfill_end
                    )
                except PolygonError as e:
                    logger.error(f"Polygon fetch failed for {ticker}: {e}")
                    raise

                self._store.write_bars(ticker, polygon_bars)
                backfilled_bars = len(polygon_bars)

                cached = self._store.read_range(
                    ticker, start, historical_end
                )

            for row in cached:
                bars.append({**row, "source": "cache"})

        # ----- Today's bar: IBKR, never cached --------------------------------
        if today <= end:
            logger.info(f"Fetching today's daily bar for {ticker} from IBKR")
            try:
                ibkr_bar = await self._ibkr.fetch_today_bar(ticker)
            except IBKRError as e:
                logger.error(f"IBKR fetch failed for {ticker}: {e}")
                raise

            if ibkr_bar is not None:
                bars.append(
                    {**self._normalize_ibkr_bar(ibkr_bar, ticker), "source": "ibkr"}
                )
            else:
                logger.info(f"IBKR returned no bar for {ticker} today")

        bars.sort(key=lambda b: b["timestamp"])

        return {
            "ticker": ticker,
            "from": start.isoformat(),
            "to": end.isoformat(),
            "backfilled_bars": backfilled_bars,
            "bars": bars,
        }

    # ------------------------------------------------------------------ write

    async def backfill_yesterday(self, ticker: str) -> int:
        """Fetch yesterday's daily bar for ``ticker`` from Polygon and persist it.

        The constituents scheduler calls this after a successful refresh, so
        the previous trading day's "final" bar is cached exactly once. Today
        is skipped: today's bar still updates intraday and is served live
        by IBKR.
        """
        ticker = ticker.upper()
        today = datetime.now(timezone.utc).date()
        yesterday = today - timedelta(days=1)
        try:
            bars = await self._polygon.fetch_daily_bars(
                ticker, yesterday, yesterday
            )
        except PolygonError as e:
            logger.error(f"Polygon fetch for {ticker} yesterday failed: {e}")
            return 0
        if not bars:
            logger.info(f"Polygon returned no bar for {ticker} on {yesterday}")
            return 0
        self._store.write_bars(ticker, bars)
        logger.info(f"Cached {ticker} {yesterday} from Polygon")
        return 1

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _missing_dates(
        start: date, end: date, cached_dates: set[date]
    ) -> list[date]:
        missing: list[date] = []
        d = start
        while d <= end:
            if d not in cached_dates:
                missing.append(d)
            d += timedelta(days=1)
        return missing

    @staticmethod
    def _normalize_ibkr_bar(bar: dict[str, Any], ticker: str) -> dict[str, Any]:
        """Reshape an IBKR bar dict to the same shape as cache / Polygon rows."""
        ts_ms = int(bar["t"])
        bar_date = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date()
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