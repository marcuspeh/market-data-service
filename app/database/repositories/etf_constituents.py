from datetime import datetime, timedelta
from typing import Any

from app.database.models.etf_constituents import ETFConstituents


class ETFConstituentsRepository:
    async def save(self, etf_symbol: str, constituents: list[dict[str, Any]]) -> None:
        # Clear old cache for this symbol
        await ETFConstituents.filter(etf_symbol=etf_symbol).delete()

        # Insert new data
        rows = [
            ETFConstituents(
                etf_symbol=etf_symbol,
                ticker=c["ticker"],
                name=c["name"],
                weight=float(c["weight"]),
            )
            for c in constituents
        ]
        await ETFConstituents.bulk_create(rows)

    async def get_cached(self, etf_symbol: str, max_age_days: int = 7) -> list[dict[str, Any]] | None:
        # Check if we have any data and how old it is
        row = (
            await ETFConstituents.filter(etf_symbol=etf_symbol)
            .order_by("-fetched_at")
            .first()
        )

        if row is None:
            return None

        fetched_at = row.fetched_at
        # Tortoise returns naive datetimes for SQLite defaults; treat as local.
        if datetime.now() - fetched_at > timedelta(days=max_age_days):
            return None

        # If valid, return all constituents
        rows = (
            await ETFConstituents.filter(etf_symbol=etf_symbol)
            .order_by("-weight")
            .values("ticker", "name", "weight")
        )
        return list(rows)