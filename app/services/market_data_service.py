import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.clients.polygon import PolygonClient, PolygonError
from app.config.settings import Settings
from app.database.models.market_bar import MarketBar
from app.database.repositories.market_bar import MarketBarRepository

logger = logging.getLogger(__name__)

SUPPORTED_TIMESPANS = {"day", "hour", "minute"}


class WindowTooLargeError(ValueError):
    """Raised when the requested window exceeds the configured cache horizon."""


class MarketDataService:
    def __init__(
        self,
        settings: Settings,
        repo: MarketBarRepository | None = None,
        polygon: PolygonClient | None = None,
    ) -> None:
        self._settings = settings
        self._repo = repo or MarketBarRepository()
        self._polygon = polygon or PolygonClient(settings)

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

        self._enforce_cache_window(start, end)

        # 1. Read whatever is already cached for this window
        cached = await self._repo.list_in_range(
            ticker, timespan, multiplier, start, end
        )

        # 2. Figure out which dates are missing and need a backfill
        cached_dates = {row["bar_date"] for row in cached}
        missing = self._missing_trading_dates(start, end, cached_dates)

        backfilled = 0
        if missing:
            logger.info(
                f"{len(missing)} missing date(s) for {ticker} "
                f"({timespan}/{multiplier}); backfilling from Polygon"
            )
            try:
                bars = await self._polygon.fetch_aggs(
                    ticker, multiplier, timespan, start, end
                )
            except PolygonError as e:
                logger.error(f"Polygon fetch failed for {ticker}: {e}")
                raise

            models = [self._to_model(ticker, timespan, multiplier, b) for b in bars]
            await self._repo.save_many(models)
            backfilled = len(models)

            # Re-read cache so the response reflects persisted rows.
            cached = await self._repo.list_in_range(
                ticker, timespan, multiplier, start, end
            )

        return {
            "ticker": ticker,
            "timespan": timespan,
            "multiplier": multiplier,
            "from": start.isoformat(),
            "to": end.isoformat(),
            "bars": cached,
            "backfilled_bars": backfilled,
        }

    # ------------------------------------------------------------------ helpers

    def _enforce_cache_window(self, start: date, end: date) -> None:
        today = datetime.now(timezone.utc).date()
        earliest_allowed = today - timedelta(days=self._settings.market_data_max_days)
        if start < earliest_allowed:
            raise WindowTooLargeError(
                f"Requested 'from' ({start}) is earlier than the cache horizon "
                f"of {self._settings.market_data_max_days} days "
                f"(earliest allowed: {earliest_allowed})."
            )

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