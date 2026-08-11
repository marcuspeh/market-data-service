from datetime import date
from typing import Any

from app.database.models.market_bar import MarketBar


class MarketBarRepository:
    """Async repository for cached OHLCV bars."""

    async def list_in_range(
        self,
        ticker: str,
        timespan: str,
        multiplier: int,
        start: date,
        end: date,
    ) -> list[dict[str, Any]]:
        rows = await (
            MarketBar.filter(
                ticker=ticker,
                timespan=timespan,
                multiplier=multiplier,
                bar_date__gte=start,
                bar_date__lte=end,
            )
            .order_by("timestamp_ms")
            .values(
                "ticker",
                "timespan",
                "multiplier",
                "timestamp_ms",
                "bar_date",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "vwap",
                "trade_count",
            )
        )
        return list(rows)

    async def existing_dates(
        self,
        ticker: str,
        timespan: str,
        multiplier: int,
        start: date,
        end: date,
    ) -> set[date]:
        rows = await MarketBar.filter(
            ticker=ticker,
            timespan=timespan,
            multiplier=multiplier,
            bar_date__gte=start,
            bar_date__lte=end,
        ).values_list("bar_date", flat=True)
        return {d for d in rows}

    async def save_many(self, bars: list[MarketBar]) -> None:
        """Bulk insert bars. Conflicts on the unique constraint are ignored
        so re-running a backfill is idempotent."""
        if not bars:
            return
        await MarketBar.bulk_create(bars, ignore_conflicts=True)