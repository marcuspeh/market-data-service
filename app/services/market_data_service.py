import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.clients.ibkr import IBKRClient, IBKRError
from app.clients.polygon import PolygonClient, PolygonError
from app.config.settings import Settings
from app.database.models.market_bar import MarketBar
from app.database.repositories.market_bar import MarketBarRepository

logger = logging.getLogger(__name__)


class MarketDataService:
    """Cache-and-proxy orchestrator for **daily** OHLCV bars.

    Strategy:
        * Historical bars (``start..today-1``) are served from the local
          MySQL cache, backfilled from Polygon on miss, and persisted.
        * Today's bar is sourced live from Interactive Brokers via
          ``IBKRClient`` and **never** persisted — the current-day bar
          may still be forming.
    """

    def __init__(
        self,
        settings: Settings,
        repo: MarketBarRepository | None = None,
        polygon: PolygonClient | None = None,
        ibkr: IBKRClient | None = None,
    ) -> None:
        self._settings = settings
        self._repo = repo or MarketBarRepository()
        self._polygon = polygon or PolygonClient(settings)
        self._ibkr = ibkr or IBKRClient(settings)

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

        # ----- Historical portion: cache + Polygon backfill -----------------
        if start <= historical_end:
            cached = await self._repo.list_in_range(
                ticker, start, historical_end
            )
            cached_timestamps = {row["timestamp"] for row in cached}
            missing = self._missing_dates(
                start, historical_end, cached_timestamps
            )

            if missing:
                # Use the smallest window that covers the missing dates so
                # we don't waste Polygon quota on already-cached days.
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

                models = [self._to_model(ticker, b) for b in polygon_bars]
                await self._repo.save_many(models)
                backfilled_bars = len(models)

                cached = await self._repo.list_in_range(
                    ticker, start, historical_end
                )

            for row in cached:
                bars.append({**row, "source": "cache"})

        # ----- Today's bar: IBKR, never cached -------------------------------
        if today <= end:
            logger.info(
                f"Fetching today's daily bar for {ticker} from IBKR"
            )
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

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _date_to_ts(d: date) -> int:
        """UTC midnight timestamp (ms) for the given calendar date."""
        return int(
            datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000
        )

    @classmethod
    def _missing_dates(
        cls, start: date, end: date, cached_timestamps: set[int]
    ) -> list[date]:
        # We don't have a holiday calendar in-app, so we treat every day
        # in the window as a candidate. Polygon simply returns nothing for
        # non-trading days, which is fine.
        missing: list[date] = []
        d = start
        while d <= end:
            if cls._date_to_ts(d) not in cached_timestamps:
                missing.append(d)
            d += timedelta(days=1)
        return missing

    @classmethod
    def _to_model(cls, ticker: str, bar: dict[str, Any]) -> MarketBar:
        return MarketBar(
            ticker=ticker,
            timestamp=int(bar["t"]),
            open=float(bar["o"]),
            high=float(bar["h"]),
            low=float(bar["l"]),
            close=float(bar["c"]),
            volume=float(bar.get("v", 0.0)),
            vwap=float(bar["vw"]) if bar.get("vw") is not None else None,
            trade_count=int(bar["n"]) if bar.get("n") is not None else None,
        )

    @staticmethod
    def _normalize_ibkr_bar(bar: dict[str, Any], ticker: str) -> dict[str, Any]:
        """Reshape an IBKR bar dict to the same shape as cached / Polygon bars."""
        return {
            "ticker": ticker,
            "timestamp": int(bar["t"]),
            "open": float(bar["o"]),
            "high": float(bar["h"]),
            "low": float(bar["l"]),
            "close": float(bar["c"]),
            "volume": float(bar.get("v", 0.0)),
            "vwap": float(bar["vw"]) if bar.get("vw") is not None else None,
            "trade_count": int(bar["n"]) if bar.get("n") is not None else None,
        }