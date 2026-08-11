"""Async wrapper over Interactive Brokers TWS / IB Gateway.

The official ``ibapi`` package is synchronous and callback-based
(``EWrapper`` / ``EClient``). This module bridges that onto asyncio by
running the client loop on a background thread and resolving an
``asyncio.Future`` from the wrapper callbacks.

Only used for **today's daily bar**: historical data comes from Polygon
+ the local cache, and the current-day bar is never persisted because
it may still be forming.
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
    """Async client for Interactive Brokers historical bars (daily only)."""

    def __init__(self, settings: Settings):
        self._settings = settings

    async def fetch_today_bar(self, ticker: str) -> dict[str, Any] | None:
        """Fetch today's single daily bar for ``ticker``.

        Returns ``None`` if the market hasn't traded yet today or IBKR
        returns no bar for the current session. Raises ``IBKRError`` on
        connection / API failure.

        The combination of ``durationStr="1 D"`` and an empty
        ``endDateTime`` asks TWS for the last 1 day of bars up to "now",
        which in practice is just today's session.
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[list[dict[str, Any]]] = loop.create_future()
        collector = _BarCollector(loop, fut)

        client = EClient(collector)
        await self._run_request(client, fut, ticker)

        bars = await fut
        return bars[-1] if bars else None

    # ------------------------------------------------------------------ helpers

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