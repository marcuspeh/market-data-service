import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.clients.ibkr import IBKRClient, IBKRError
from app.clients.polygon import PolygonClient, PolygonError
from app.config.settings import Settings
from app.database.models.market_bar import MarketBar
from app.database.repositories.market_bar import MarketBarRepository

logger = logging.getLogger(__name__)

SUPPORTED_TIMESPANS = {"day", "hour", "minute"}


class MarketDataService:
    """Cache-and-proxy orchestrator for OHLCV bars.

    Strategy:
        * Bars from the **historical** portion of the window (``start..today-1``)
          are served from the local MySQL cache, backfilled from Polygon on miss.
        * Bars for **today** (and any future date in the window) are sourced
          live from Interactive Brokers via ``IBKRClient`` and **never**
          persisted — the current-day bar is still forming and will change.
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
        timespan: str = "day",
        multiplier: int = 1,
    ) -> dict[str, Any]:
        ticker = ticker.upper()
        timespan = timespan.lower()

        if timespan not in SUPPORTED_TIMESPANS:
            raise ValueError(
                f"Unsupported timespan '{timespan}'. "
                f"Allowed: {sorted(SUPPORTED_TIMESPANS)}"
            )
        if multiplier <= 0:
            raise ValueError("multiplier must be a positive integer")
        if end < start:
            raise ValueError("'to' must be on or after 'from'")

        today = datetime.now(timezone.utc).date()
        historical_end = min(end, today - timedelta(days=1))
        today_start = max(start, today)

        # ----- Historical portion: cache + Polygon backfill -----------------
        bars: list[dict[str, Any]] = []
        backfilled_bars = 0

        if start <= historical_end:
            cached = await self._repo.list_in_range(
                ticker, timespan, multiplier, start, historical_end
            )
            cached_dates = {row["bar_date"] for row in cached}
            missing = self._missing_trading_dates(
                start, historical_end, cached_dates
            )

            if missing:
                logger.info(
                    f"{len(missing)} missing date(s) for {ticker} "
                    f"({timespan}/{multiplier}); backfilling from Polygon"
                )
                try:
                    polygon_bars = await self._polygon.fetch_aggs(
                        ticker, multiplier, timespan, start, historical_end
                    )
                except PolygonError as e:
                    logger.error(f"Polygon fetch failed for {ticker}: {e}")
                    raise

                models = [
                    self._to_model(ticker, timespan, multiplier, b)
                    for b in polygon_bars
                ]
                await self._repo.save_many(models)
                backfilled_bars = len(models)

                cached = await self._repo.list_in_range(
                    ticker, timespan, multiplier, start, historical_end
                )

            # Mark each cached bar with its provenance.
            for row in cached:
                bars.append({**row, "source": "cache"})

        # ----- Live portion: IBKR, today (and any future dates) ---------------
        if today_start <= end:
            logger.info(
                f"Fetching live bars for {ticker} ({timespan}/{multiplier}) "
                f"from IBKR for {today_start}..{end}"
            )
            try:
                ibkr_bars = await self._ibkr.fetch_intraday_bars(
                    ticker, multiplier, timespan
                )
            except IBKRError as e:
                logger.error(f"IBKR fetch failed for {ticker}: {e}")
                raise

            for bar in ibkr_bars:
                bars.append(
                    {
                        **self._normalize_ibkr_bar(bar, ticker, timespan, multiplier),
                        "source": "ibkr",
                    }
                )

        # Final response: sort by timestamp ascending.
        bars.sort(key=lambda b: b["timestamp_ms"])

        return {
            "ticker": ticker,
            "timespan": timespan,
            "multiplier": multiplier,
            "from": start.isoformat(),
            "to": end.isoformat(),
            "backfilled_bars": backfilled_bars,
            "bars": bars,
        }

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _missing_trading_dates(
        start: date, end: date, cached_dates: set[date]
    ) -> list[date]:
        # We don't have a holiday calendar in-app, so we treat every day in the
        # window as a candidate. Polygon simply returns nothing for non-trading
        # days, which is fine.
        missing: list[date] = []
        d = start
        while d <= end:
            if d not in cached_dates:
                missing.append(d)
            d += timedelta(days=1)
        return missing

    @staticmethod
    def _to_model(
        ticker: str, timespan: str, multiplier: int, bar: dict[str, Any]
    ) -> MarketBar:
        ts_ms = int(bar["t"])
        bar_date = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date()
        return MarketBar(
            ticker=ticker,
            timespan=timespan,
            multiplier=multiplier,
            timestamp_ms=ts_ms,
            bar_date=bar_date,
            open=float(bar["o"]),
            high=float(bar["h"]),
            low=float(bar["l"]),
            close=float(bar["c"]),
            volume=float(bar.get("v", 0.0)),
            vwap=float(bar["vw"]) if bar.get("vw") is not None else None,
            trade_count=int(bar["n"]) if bar.get("n") is not None else None,
        )

    @staticmethod
    def _normalize_ibkr_bar(
        bar: dict[str, Any], ticker: str, timespan: str, multiplier: int
    ) -> dict[str, Any]:
        """Reshape an IBKR bar dict to the same shape as cached / Polygon bars.

        Note: ``bar_date`` is omitted from cached bars to match what
        ``MarketBarRepository.list_in_range`` returns (it projects a tuple
        of fields that doesn't include bar_date). For IBKR we derive it from
        the bar timestamp so the response stays self-consistent.
        """
        ts_ms = int(bar["t"])
        bar_date = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date()
        return {
            "ticker": ticker,
            "timespan": timespan,
            "multiplier": multiplier,
            "timestamp_ms": ts_ms,
            "bar_date": bar_date,
            "open": float(bar["o"]),
            "high": float(bar["h"]),
            "low": float(bar["l"]),
            "close": float(bar["c"]),
            "volume": float(bar.get("v", 0.0)),
            "vwap": float(bar["vw"]) if bar.get("vw") is not None else None,
            "trade_count": int(bar["n"]) if bar.get("n") is not None else None,
        }