import logging
from typing import Any

from app.database.repositories.etf_constituents import ETFConstituentsRepository
from app.services.constituents_fetcher import fetch_spy_constituents

logger = logging.getLogger(__name__)


class ConstituentsService:
    SUPPORTED_SYMBOLS = {"SPY"}

    def __init__(self, repo: ETFConstituentsRepository | None = None) -> None:
        self._repo = repo or ETFConstituentsRepository()

    async def get_constituents(self, symbol: str) -> dict[str, Any]:
        symbol = symbol.upper()

        if symbol not in self.SUPPORTED_SYMBOLS:
            raise UnsupportedSymbolError(symbol, self.SUPPORTED_SYMBOLS)

        # 1. Check cache (7 days TTL)
        cached = await self._repo.get_cached(symbol)
        if cached:
            logger.info(f"Returning cached constituents for {symbol}")
            return {"symbol": symbol, "constituents": cached, "source": "cache"}

        # 2. Fetch fresh data if cache is missing or expired
        logger.info(f"Cache miss/expired for {symbol}. Fetching fresh data...")
        fresh = await fetch_spy_constituents()

        # 3. Update cache
        await self._repo.save(symbol, fresh)
        logger.info(f"Updated cache for {symbol}")

        return {"symbol": symbol, "constituents": fresh, "source": "external"}


class UnsupportedSymbolError(ValueError):
    def __init__(self, symbol: str, supported: set[str]) -> None:
        self.symbol = symbol
        self.supported = supported
        super().__init__(
            f"Symbol '{symbol}' is not supported. "
            f"Only {sorted(supported)} are supported currently."
        )