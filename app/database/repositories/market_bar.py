from datetime import date, datetime, timezone
from typing import Any

from app.database.models.market_bar import MarketBar


def _date_to_ts(d: date) -> int:
    """UTC midnight timestamp (ms) for the given calendar date."""
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


class MarketBarRepository:
    """Async repository for cached daily OHLCV bars."""

    async def list_in_range(
        self,
        ticker: str,
        start: date,
        end: date,
    ) -> list[dict[str, Any]]:
        start_ts = _date_to_ts(start)
        end_ts = _date_to_ts(end)
        rows = await (
            MarketBar.filter(
                ticker=ticker,
                timestamp__gte=start_ts,
                timestamp__lte=end_ts,
            )
            .order_by("timestamp")
            .values(
                "ticker",
                "timestamp",
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

    async def existing_timestamps(
        self,
        ticker: str,
        start: date,
        end: date,
    ) -> set[int]:
        start_ts = _date_to_ts(start)
        end_ts = _date_to_ts(end)
        rows = await MarketBar.filter(
            ticker=ticker,
            timestamp__gte=start_ts,
            timestamp__lte=end_ts,
        ).values_list("timestamp", flat=True)
        return {ts for ts in rows}

    async def save_many(self, bars: list[MarketBar]) -> None:
        """Bulk insert bars. Conflicts on the unique constraint are ignored
        so re-running a backfill is idempotent."""
        if not bars:
            return
        await MarketBar.bulk_create(bars, ignore_conflicts=True)