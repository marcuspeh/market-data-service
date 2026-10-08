"""Constituents service: read snapshots from a parquet store and refresh
them on schedule. Refresh is driven by the APScheduler job in
:mod:`app.services.constituents_scheduler` (runs every day at 8:30 ET
and calls :meth:`refresh_symbol` / :meth:`refresh_all`).
"""
import bisect
from datetime import date, timedelta
from typing import Any

from app.config.settings import get_settings
from app.logging_setup import client
from app.services.constituents_fetcher import (
    ETF_REGISTRY,
    fetch_etf_constituents,
)
from app.services.constituents_store import (
    ConstituentsNotFoundError,
    ConstituentsStore,
)

log = client()


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

        log.info(
            "Returning %d constituents for %s on %s",
            len(tickers), symbol, snapshot_date,
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

        log.info("Refreshing %s constituents for %s", symbol, snapshot_date)
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
                log.error("Failed to refresh %s: %s", symbol, e)
                results[symbol] = -1
        return results

    # Number of past calendar days to scan for missing snapshots,
    # excluding today. The window is [today - GAP_SCAN_DAYS, today - 1].
    GAP_SCAN_DAYS = 7

    def backfill_gaps_in_recent_window(
        self, today: date | None = None
    ) -> dict[str, str]:
        """Plug any snapshot gaps in the past ``GAP_SCAN_DAYS`` days for
        every supported symbol.

        The scan window is the 7 calendar days ending yesterday:
        ``[today - 7, today - 1]``. Today is excluded because
        :meth:`refresh_symbol` / :meth:`refresh_all` are responsible
        for writing the current-day snapshot. Real, already-written
        snapshots are never overwritten — this method only fills gaps
        (idempotent on second call).

        For each missing date in the window, the most recent available
        prior snapshot is copied forward. The source date is found by
        walking backward day-by-day from the gap, so a contiguous run
        of missing days still gets filled as long as any older history
        exists. If the source cannot be found at all, that gap is left
        alone and the symbol's result reports ``"no_source_data"``.

        Returns ``{symbol: action}`` where ``action`` is one of:

        * ``"already_present"`` — the 7-day window had no gaps.
        * ``"backfilled:{n}_gaps"`` — ``n`` gaps were filled (n ≥ 1).
        * ``"no_source_data"`` — at least one gap had no prior data to
          copy from; remaining gaps were skipped.
        * ``"error"`` — an unexpected error prevented the scan.
        """
        if today is None:
            today = get_settings().now_ny_date()

        # Past GAP_SCAN_DAYS days, ending yesterday (today is excluded).
        window_end = today - timedelta(days=1)
        window_start = today - timedelta(days=self.GAP_SCAN_DAYS)

        results: dict[str, str] = {}
        for symbol in self.SUPPORTED_SYMBOLS:
            try:
                missing = [
                    window_start + timedelta(days=offset)
                    for offset in range(self.GAP_SCAN_DAYS)
                    if not self._store.has_snapshot(
                        symbol, window_start + timedelta(days=offset)
                    )
                ]

                if not missing:
                    log.info(
                        "No gaps in the past %d days for %s; skipping",
                        self.GAP_SCAN_DAYS, symbol,
                    )
                    results[symbol] = "already_present"
                    continue

                filled = 0
                no_source = False
                for gap_date in missing:
                    source = self._find_prior_snapshot(symbol, gap_date)
                    if source is None:
                        log.error(
                            "Cannot backfill %s for %s: no prior snapshot",
                            symbol, gap_date,
                        )
                        no_source = True
                        continue
                    source_date, tickers = source
                    self._store.write_snapshot(symbol, gap_date, tickers)
                    log.info(
                        "Backfilled %s for %s using %s data (%d tickers)",
                        symbol, gap_date, source_date, len(tickers),
                    )
                    filled += 1

                if filled == 0:
                    results[symbol] = "no_source_data"
                elif no_source:
                    # Some gaps were filled but at least one was not —
                    # surface the partial state.
                    results[symbol] = f"backfilled:{filled}_gaps"
                else:
                    results[symbol] = f"backfilled:{filled}_gaps"
            except Exception as e:  # noqa: BLE001 — best-effort backfill
                log.error("Backfill failed for %s: %s", symbol, e)
                results[symbol] = "error"
        return results

    def _find_prior_snapshot(
        self, symbol: str, target_date: date
    ) -> tuple[date, list[str]] | None:
        """Walk backward from ``target_date - 1`` until a snapshot is
        found. Returns ``(source_date, tickers)`` or ``None`` if no
        prior snapshot exists for ``symbol``.

        We use :meth:`ConstituentsStore.list_snapshot_dates` so the
        whole search is a single parquet read + an in-memory
        bisection, rather than a day-by-day read_snapshot probe."""
        all_dates = self._store.list_snapshot_dates(symbol)
        if not all_dates:
            return None
        # list_snapshot_dates returns ascending order. Find the largest
        # date strictly less than target_date via bisect.
        idx = bisect.bisect_left(all_dates, target_date) - 1
        if idx < 0:
            return None
        source_date = all_dates[idx]
        try:
            tickers = self._store.read_snapshot(symbol, source_date)
        except ConstituentsNotFoundError:  # pragma: no cover — paranoia
            return None
        return source_date, tickers

    # Kept as a thin shim for the scheduler, which previously called a
    # T-1-only helper. Now routes to the 7-day gap scan.
    def backfill_missing_t1_from_t2(
        self, today: date | None = None
    ) -> dict[str, str]:
        return self.backfill_gaps_in_recent_window(today=today)