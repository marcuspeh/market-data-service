"""Constituents service: read snapshots from a parquet store and refresh
them on schedule. Refresh is driven by the APScheduler job in
:mod:`app.services.constituents_scheduler` (runs every day at 8:30 ET
and calls :meth:`refresh_symbol` / :meth:`refresh_all`).
"""
import logging
from datetime import date
from typing import Any

from app.config.settings import get_settings
from app.services.constituents_fetcher import (
    ETF_REGISTRY,
    fetch_etf_constituents,
)
from app.services.constituents_store import (
    ConstituentsNotFoundError,
    ConstituentsStore,
)

logger = logging.getLogger(__name__)


class UnsupportedSymbolError(ValueError):
    def __init__(self, symbol: str, supported: set[str]) -> None:
        self.symbol = symbol
        self.supported = supported
        super().__init__(
            f"Symbol '{symbol}' is not supported. "
            f"Only {sorted(supported)} are supported currently."
        )


class SnapshotNotFoundError(KeyError):
    """Raised when no snapshot exists for the requested (symbol, date)."""

    def __init__(self, symbol: str, snapshot_date: date) -> None:
        self.symbol = symbol
        self.snapshot_date = snapshot_date
        super().__init__(
            f"No constituents snapshot for {symbol} on {snapshot_date}"
        )


class ConstituentsService:
    SUPPORTED_SYMBOLS: set[str] = set(ETF_REGISTRY)

    def __init__(self, store: ConstituentsStore | None = None) -> None:
        if store is not None:
            self._store = store
        else:
            self._store = ConstituentsStore(get_settings().constituents_dir)

    def get_constituents(self, symbol: str, snapshot_date: date) -> dict[str, Any]:
        symbol = symbol.upper()
        if symbol not in self.SUPPORTED_SYMBOLS:
            raise UnsupportedSymbolError(symbol, self.SUPPORTED_SYMBOLS)

        try:
            tickers = self._store.read_snapshot(symbol, snapshot_date)
        except ConstituentsNotFoundError as e:
            raise SnapshotNotFoundError(symbol, snapshot_date) from e

        logger.info(
            f"Returning {len(tickers)} constituents for {symbol} on {snapshot_date}"
        )
        return {
            "symbol": symbol,
            "date": snapshot_date.isoformat(),
            "constituents": tickers,
            "source": "parquet",
        }

    async def refresh_symbol(self, symbol: str, snapshot_date: date) -> int:
        """Fetch live holdings for ``symbol`` and persist a snapshot.
        Returns the number of holding rows written."""
        symbol = symbol.upper()
        if symbol not in self.SUPPORTED_SYMBOLS:
            raise UnsupportedSymbolError(symbol, self.SUPPORTED_SYMBOLS)

        logger.info(f"Refreshing {symbol} constituents for {snapshot_date}")
        holdings = await fetch_etf_constituents(symbol)
        tickers = [row["ticker"] for row in holdings]
        self._store.write_snapshot(symbol, snapshot_date, tickers)
        return len(tickers)

    async def refresh_all(self, snapshot_date: date) -> dict[str, int]:
        """Refresh every supported ticker. Returns ``{symbol: row_count}``."""
        results: dict[str, int] = {}
        for symbol in self.SUPPORTED_SYMBOLS:
            try:
                results[symbol] = await self.refresh_symbol(symbol, snapshot_date)
            except Exception as e:  # noqa: BLE001 — best-effort refresh
                logger.error(f"Failed to refresh {symbol}: {e}")
                results[symbol] = -1
        return results