"""Async wrapper over the Longbridge OpenAPI QuoteContext for today's daily bar."""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import date, datetime, timezone
from typing import Any

from longbridge.openapi import AdjustType, Config, Period, QuoteContext  # type: ignore[import-not-found]

from app.config.settings import NY_TZ, Settings, ny_now

logger = logging.getLogger(__name__)

DEFAULT_CACHE_TTL_SECONDS = 30.0  # 30 seconds


class LongbridgeError(RuntimeError):
    pass


class LongbridgeClient:
    """Async client for Longbridge daily history bars with a 5-minute in-process TTL."""

    def __init__(
        self,
        settings: Settings,
        *,
        cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    ) -> None:
        self._settings = settings
        self._cache_ttl_seconds = cache_ttl_seconds

        self._cache: dict[tuple[str, date], tuple[dict[str, Any] | None, float]] = {}
        self._inflight: dict[tuple[str, date], asyncio.Future] = {}
        self._lock = threading.Lock()

        # Lazy so the service boots without LONGBRIDGE_* credentials.
        self._ctx: Any | None = None

    def _ensure_ctx(self) -> Any:
        if self._ctx is None:
            self._ctx = QuoteContext(Config.from_apikey_env())
        return self._ctx

    async def fetch_today_bar(self, ticker: str) -> dict[str, Any] | None:
        """Fetch today's single daily bar for ``ticker``; returns None if none yet."""
        key = (ticker.upper(), self._today_ny())
        now = time.monotonic()

        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and cached[1] > now:
                logger.debug(f"Longbridge cache hit for {ticker}")
                return cached[0]
            self._cache.pop(key, None)

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

    async def _fetch_today_bar_uncached(self, ticker: str) -> dict[str, Any] | None:
        """Pull the most recent daily candle on a worker thread."""
        symbol = self._format_symbol(ticker)
        timeout = self._settings.longbridge_timeout_seconds

        def _call() -> list[Any]:
            return self._ensure_ctx().history_candlesticks_by_offset(
                symbol,
                Period.Day,
                AdjustType.NoAdjust,
                True,  # forward: query from the offset towards latest
                2,
            )

        try:
            candles = await asyncio.wait_for(
                asyncio.to_thread(_call), timeout=timeout
            )
        except asyncio.TimeoutError as e:
            raise LongbridgeError(
                f"Longbridge request for {ticker} timed out after {timeout}s"
            ) from e
        except Exception as e:
            raise LongbridgeError(f"Longbridge request for {ticker} failed: {e}") from e

        if not candles:
            return None
        last = candles[-1]
        ts_ms = self._candle_ts_ms(last)
        return {
            "t": ts_ms,
            "o": float(last.open),
            "h": float(last.high),
            "l": float(last.low),
            "c": float(last.close),
            "v": float(last.volume or 0),
            "vw": float(last.turnover) if last.turnover is not None else None,
            "n": None,  # Longbridge daily bars don't expose trade count
        }

    def _format_symbol(self, ticker: str) -> str:
        """Map a bare US ticker to Longbridge's ``<TICKER>.US`` form."""
        if "." in ticker:
            return ticker.upper()
        return f"{ticker.upper()}{self._settings.longbridge_region_suffix}"

    @staticmethod
    def _candle_ts_ms(candle: Any) -> int:
        """Return NY-midnight epoch-ms for ``candle.timestamp``."""
        ts = candle.timestamp
        if isinstance(ts, datetime):
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            ts = ts.astimezone(timezone.utc)
        else:  # pragma: no cover - defensive for future SDK shapes
            ts = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        if NY_TZ is None:
            return int(ts.timestamp() * 1000)
        ny_date = ts.astimezone(NY_TZ).date()
        return int(
            datetime(ny_date.year, ny_date.month, ny_date.day, tzinfo=NY_TZ)
            .astimezone(timezone.utc)
            .timestamp()
            * 1000
        )

    @staticmethod
    def _today_ny() -> date:
        """Today on the Nasdaq (US/Eastern) trading calendar."""
        return ny_now().date()