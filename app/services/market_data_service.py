"""Daily OHLCV bar orchestrator: parquet cache + Polygon backfill + Longbridge live bar."""
import logging
from datetime import date, datetime, timedelta
from typing import Any

from app.clients.longbridge import LongbridgeClient, LongbridgeError
from app.clients.polygon import PolygonClient, PolygonError
from app.config.settings import Settings, ny_from_ts
from app.services.market_bars_store import MarketBarsStore

logger = logging.getLogger(__name__)


class MarketDataService:
    def __init__(
        self,
        settings: Settings,
        store: MarketBarsStore | None = None,
        polygon: PolygonClient | None = None,
        longbridge: LongbridgeClient | None = None,
    ) -> None:
        self._settings = settings
        self._store = store or MarketBarsStore(settings.market_data_dir)
        self._polygon = polygon or PolygonClient(settings)
        self._longbridge = longbridge or LongbridxgeClient(settings)

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

        bars: list[dict[str, Any]] = []
        backfilled_bars = 0

        if start <= historical_end:
            # Drop any cached intraday row whose date is today.
            cached = self._store.read_range(ticker, start, historical_end)
            cached = [r for r in cached if r["date"] < today]
            cached_dates = {row["date"] for row in cached}
            missing = self._missing_dates(start, historical_end, cached_dates)

            if missing:
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

                # Filter Polygon's today-bar (intraday) before persisting.
                persistable = [
                    b for b in polygon_bars if self._bar_date_ny(b) < today
                ]
                if persistable:
                    self._store.write_bars(ticker, persistable)
                backfilled_bars = len(persistable)

                cached = self._store.read_range(ticker, start, historical_end)
                cached = [r for r in cached if r["date"] < today]

            for row in cached:
                bars.append({**row, "source": "cache"})

        if today <= end:
            logger.info(f"Fetching today's daily bar for {ticker} from Longbridge")
            try:
                today_bar = await self._longbridge.fetch_today_bar(ticker)
            except LongbridgeError as e:
                logger.error(f"Longbridge fetch failed for {ticker}: {e}")
                raise

            if today_bar is not None:
                bars.append(
                    {**self._normalize_today_bar(today_bar, ticker), "source": "longbridge"}
                )
            else:
                logger.info(f"Longbridge returned no bar for {ticker} today")

        bars.sort(key=lambda b: b["timestamp"])

        return {
            "ticker": ticker,
            "from": start.isoformat(),
            "to": end.isoformat(),
            "backfilled_bars": backfilled_bars,
            "bars": bars,
        }

    async def backfill_yesterday(self, ticker: str) -> int:
        """Fetch yesterday's daily bar from Polygon and persist it."""
        ticker = ticker.upper()
        today = Settings.now_ny_date()
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