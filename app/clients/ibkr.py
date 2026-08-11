"""Async wrapper over Interactive Brokers TWS / IB Gateway.

The official ``ibapi`` package is synchronous and callback-based
(``EWrapper`` / ``EClient``). This module bridges that onto asyncio by
running the client loop on a background thread and resolving an
``asyncio.Future`` from the wrapper callbacks.

Only used for **current-day** bars: historical data from IBKR is not
persisted to the cache because the current-day bar may still be forming.
"""
import asyncio
import logging
import threading
from datetime import datetime, timezone
from typing import Any

from ibapi.client import EClient
from ibapi.contract import Contract
from ibapi.wrapper import EWrapper

from app.config.settings import Settings

logger = logging.getLogger(__name__)


class IBKRError(RuntimeError):
    pass


# Map our (multiplier, timespan) vocabulary to ibapi's ``barSizeSetting``.
# ibapi doesn't have an "1 hour" bar for example, so we degrade to the
# closest supported granularity.
_BAR_SIZE_MAP: dict[tuple[int, str], str] = {
    (1, "second"): "1 secs",
    (5, "second"): "5 secs",
    (15, "second"): "15 secs",
    (30, "second"): "30 secs",
    (1, "minute"): "1 min",
    (2, "minute"): "2 mins",
    (3, "minute"): "3 mins",
    (5, "minute"): "5 mins",
    (15, "minute"): "15 mins",
    (30, "minute"): "30 mins",
    (1, "hour"): "1 hour",
    (1, "day"): "1 day",
}


def _to_bar_size(multiplier: int, timespan: str) -> str:
    timespan = timespan.lower()
    key = (multiplier, timespan)
    if key in _BAR_SIZE_MAP:
        return _BAR_SIZE_MAP[key]
    raise IBKRError(
        f"IBKR does not support {multiplier} {timespan} bars. "
        f"Supported: {sorted(_BAR_SIZE_MAP)}"
    )


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
        self._error: IBKRError | None = None

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
                    "vw": float(getattr(bar, "average", 0) or 0)
                    if getattr(bar, "average", None)
                    else None,
                    "n": int(getattr(bar, "barCount", 0) or 0)
                    if getattr(bar, "barCount", None)
                    else None,
                }
            )
        except (ValueError, TypeError) as e:
            logger.warning(f"Skipping malformed IBKR bar: {e}")

    def historicalDataEnd(self, reqId, start, end):
        if not self._fut.done():
            self._loop.call_soon_threadsafe(
                self._fut.set_result, self.bars
            )

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        # Errors during connection come through here too.
        msg = f"IBKR error {errorCode}: {errorString}"
        logger.warning(msg)
        if not self._fut.done():
            self._loop.call_soon_threadsafe(
                self._fut.set_exception, IBKRError(msg)
            )

    @staticmethod
    def _parse_bar_timestamp_ms(raw: str) -> int:
        """ibapi returns bar timestamps as ``"YYYYMMDD HH:MM:SS"`` (UTC) or
        just ``"YYYYMMDD"`` for daily bars. Convert to epoch milliseconds."""
        raw = raw.strip()
        for fmt in ("%Y%m%d %H:%M:%S", "%Y%m%d"):
            try:
                dt = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
                return int(dt.timestamp() * 1000)
            except ValueError:
                continue
        raise ValueError(f"Unrecognised IBKR bar timestamp: {raw!r}")


class IBKRClient:
    """Async client for Interactive Brokers historical bars."""

    def __init__(self, settings: Settings):
        self._settings = settings

    async def fetch_intraday_bars(
        self,
        ticker: str,
        multiplier: int,
        timespan: str,
    ) -> list[dict[str, Any]]:
        """Fetch intraday bars for the **current trading day only** (today).

        IBKR is intentionally only used for today's data:
            - Polygon + the local cache covers all historical data;
              using IBKR there would burn the upstream rate limit
              unnecessarily.
            - The current-day bar is still forming and may revise
              intraday, so the result is **never** persisted.

        The combination of ``durationStr="1 D"`` and an empty
        ``endDateTime`` asks TWS for the last 1 day of bars up to "now",
        which in practice is just today's session.
        """
        bar_size = _to_bar_size(multiplier, timespan)

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[list[dict[str, Any]]] = loop.create_future()
        collector = _BarCollector(loop, fut)

        client = EClient(collector)
        await self._run_request(client, collector, fut, ticker, bar_size)

        return await fut

    # ------------------------------------------------------------------ helpers

    async def _run_request(
        self,
        client: EClient,
        collector: _BarCollector,
        fut: asyncio.Future,
        ticker: str,
        bar_size: str,
    ) -> None:
        """Connect to TWS/IB Gateway, request bars, then disconnect.

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

        def _connect_and_run():
            try:
                client.connect(host, port, clientId=client_id)
                # reqHistoricalData end param: pass "" to get up to "now".
                client.reqHistoricalData(
                    reqId=1,
                    contract=contract,
                    endDateTime="",
                    durationStr="1 D",
                    barSizeSetting=bar_size,
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