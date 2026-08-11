"""Async wrapper over Interactive Brokers TWS / IB Gateway.

The official ``ibapi`` package is synchronous and callback-based
(``EWrapper`` / ``EClient``). This module bridges that onto asyncio by
running the client loop on a background thread and resolving an
``asyncio.Future`` from the wrapper callbacks.

Only used for **today's daily bar**: historical data comes from Polygon
+ the local cache, and the current-day bar is never persisted to the
MySQL cache (it may still be forming). Instead, ``IBKRClient`` keeps a
short-lived in-process cache so repeated calls within the same 5-minute
window don't burn IBKR's upstream rate limit.
"""
import asyncio
import logging
import threading
import time
from datetime import date, datetime, timezone
from typing import Any

from ibapi.client import EClient
from ibapi.contract import Contract
from ibapi.wrapper import EWrapper

from app.config.settings import Settings

logger = logging.getLogger(__name__)

# In-process TTL for today's bar. Short enough that the bar can keep
# updating intraday without serving stale data for long.
DEFAULT_CACHE_TTL_SECONDS = 300.0  # 5 minutes


class IBKRError(RuntimeError):
    pass


class _BarCollector(EWrapper):
    """Captures historical bars and signals completion via an asyncio.Future.

    NOTE: ``ibapi`` calls these methods from its own background thread. The
    loop that owns the future is captured at construction time so we can
    call ``call_soon_threadsafe`` to set the result without race conditions.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, fut: asyncio.Future):
        super().__init__()
        self._loop = loop
        self._fut = fut
        self.bars: list[dict[str, Any]] = []

    # --- EWrapper callbacks --------------------------------------------------

    def historicalData(self, reqId, bar):
        # bar is a BarData namedtuple with .date, .open, .high, .low, .close,
        # .volume, .barCount, .average (vwap).
        try:
            self.bars.append(
                {
                    "t": self._parse_bar_timestamp_ms(bar.date),
                    "o": float(bar.open),
                    "h": float(bar.high),
                    "l": float(bar.low),
                    "c": float(bar.close),
                    "v": float(getattr(bar, "volume", 0) or 0),
                    "vw": (
                        float(getattr(bar, "average", 0) or 0)
                        if getattr(bar, "average", None)
                        else None
                    ),
                    "n": (
                        int(getattr(bar, "barCount", 0) or 0)
                        if getattr(bar, "barCount", None)
                        else None
                    ),
                }
            )
        except (ValueError, TypeError) as e:
            logger.warning(f"Skipping malformed IBKR bar: {e}")

    def historicalDataEnd(self, reqId, start, end):
        if not self._fut.done():
            self._loop.call_soon_threadsafe(self._fut.set_result, self.bars)

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        # Errors during connection come through here too.
        msg = f"IBKR error {errorCode}: {errorString}"
        logger.warning(msg)
        if not self._fut.done():
            self._loop.call_soon_threadsafe(self._fut.set_exception, IBKRError(msg))

    @staticmethod
    def _parse_bar_timestamp_ms(raw: str) -> int:
        """ibapi returns daily bar timestamps as ``"YYYYMMDD"``. Convert to
        epoch milliseconds (UTC midnight)."""
        raw = raw.strip()
        for fmt in ("%Y%m%d %H:%M:%S", "%Y%m%d"):
            try:
                dt = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
                return int(dt.timestamp() * 1000)
            except ValueError:
                continue
        raise ValueError(f"Unrecognised IBKR bar timestamp: {raw!r}")


class IBKRClient:
    """Async client for Interactive Brokers historical bars (daily only).

    Wraps a short-lived in-process TTL cache (default 5 minutes) around
    the underlying TWS round-trip so concurrent requests for the same
    ticker collapse into a single upstream call.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    ):
        self._settings = settings
        self._cache_ttl_seconds = cache_ttl_seconds

        # _cache: (ticker, date) -> (bar, expires_at_monotonic)
        self._cache: dict[tuple[str, date], tuple[dict[str, Any] | None, float]] = {}
        # In-flight requests: (ticker, date) -> Future.
        # Prevents N concurrent calls for the same key from issuing N
        # upstream IBKR requests.
        self._inflight: dict[tuple[str, date], asyncio.Future] = {}
        self._lock = threading.Lock()

    async def fetch_today_bar(self, ticker: str) -> dict[str, Any] | None:
        """Fetch today's single daily bar for ``ticker``.

        Returns ``None`` if the market hasn't traded yet today or IBKR
        returns no bar for the current session. Raises ``IBKRError`` on
        connection / API failure.

        Results are cached in-process for ``cache_ttl_seconds`` (default
        5 minutes) — so repeated calls within the window are served
        from memory. The cache key includes the calendar date so a
        stale ``yesterday`` bar cannot leak into ``today``.
        """
        key = (ticker.upper(), self._today_utc())
        now = time.monotonic()

        # Cache hit?
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and cached[1] > now:
                logger.debug(f"IBKR cache hit for {ticker}")
                return cached[0]
            # Expired — drop it
            self._cache.pop(key, None)

        # Single-flight: if another coroutine is already fetching this
        # key, await its future instead of issuing a duplicate request.
        loop = asyncio.get_running_loop()
        with self._lock:
            inflight = self._inflight.get(key)
            if inflight is not None:
                is_owner = False
                future = inflight
            else:
                future = loop.create_future()
                self._inflight[key] = future
                is_owner = True

        if not is_owner:
            # We don't `await future` inside the `with` block to avoid
            # holding the lock during the await.
            return await future

        try:
            bar = await self._fetch_today_bar_uncached(ticker)
        except BaseException as e:
            with self._lock:
                self._inflight.pop(key, None)
                if not future.done():
                    future.set_exception(e)
            raise
        else:
            with self._lock:
                self._cache[key] = (bar, now + self._cache_ttl_seconds)
                self._inflight.pop(key, None)
                if not future.done():
                    future.set_result(bar)
            return bar

    # ------------------------------------------------------------------ helpers

    async def _fetch_today_bar_uncached(self, ticker: str) -> dict[str, Any] | None:
        """The actual TWS round-trip — no caching, no single-flight."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[list[dict[str, Any]]] = loop.create_future()
        collector = _BarCollector(loop, fut)

        client = EClient(collector)
        await self._run_request(client, fut, ticker)

        bars = await fut
        return bars[-1] if bars else None

    @staticmethod
    def _today_utc() -> date:
        return datetime.now(timezone.utc).date()

    async def _run_request(
        self,
        client: EClient,
        fut: asyncio.Future,
        ticker: str,
    ) -> None:
        """Connect to TWS/IB Gateway, request today's bar, then disconnect.

        Runs the synchronous ``client.run()`` loop on a background thread
        so the asyncio event loop stays unblocked.
        """
        host = self._settings.ibkr_host
        port = self._settings.ibkr_port
        client_id = self._settings.ibkr_client_id
        timeout = self._settings.ibkr_timeout_seconds

        contract = Contract()
        contract.symbol = ticker.upper()
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.currency = "USD"

        loop = asyncio.get_running_loop()

        def _connect_and_run():
            try:
                client.connect(host, port, clientId=client_id)
                # reqHistoricalData end param: pass "" to get up to "now".
                client.reqHistoricalData(
                    reqId=1,
                    contract=contract,
                    endDateTime="",
                    durationStr="1 D",
                    barSizeSetting="1 day",
                    whatToShow="TRADES",
                    useRTH=1,
                    formatDate=1,
                    keepUpToDate=False,
                    chartOptions=[],
                )
                client.run()
            except Exception as e:
                if not fut.done():
                    loop.call_soon_threadsafe(
                        fut.set_exception, IBKRError(f"IBKR thread error: {e}")
                    )

        thread = threading.Thread(
            target=_connect_and_run, name=f"ibkr-{ticker}", daemon=True
        )
        thread.start()

        try:
            await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            raise IBKRError(
                f"IBKR request for {ticker} timed out after {timeout}s"
            )
        finally:
            try:
                client.disconnect()
            except Exception:
                pass
            thread.join(timeout=2)