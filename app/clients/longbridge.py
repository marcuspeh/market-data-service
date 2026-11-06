"""Async wrapper over the Longbridge OpenAPI QuoteContext for today's daily bar."""
from __future__ import annotations

import asyncio
import threading
import time
from datetime import date, datetime, timezone
from typing import Any

from longbridge.openapi import AdjustType, Config, Period, QuoteContext  # type: ignore[import-not-found]

from app.config.settings import NY_TZ, Settings, ny_from_ts, ny_now
from app.logging_setup import client

log = client()

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
            self._ctx = QuoteContext(
                Config.from_apikey(
                    self._settings.longbridge_app_key,
                    self._settings.longbridge_app_secret,
                    self._settings.longbridge_access_token,
                    http_url=self._http_url(),
                )
            )
        return self._ctx

    def _http_url(self) -> str | None:
        """REST endpoint for the configured region.

        The SDK resolves its endpoint from the ``LONGBRIDGE_REGION``
        environment variable, but pydantic-settings loads ``.env`` into
        the settings object only — it never exports to ``os.environ``.
        The region is therefore translated to the endpoint URL here and
        passed explicitly, so ``.env`` actually takes effect.

        ``cn`` selects the mainland-China endpoints; everything else
        uses the global endpoints.
        """
        region = (self._settings.longbridge_region or "").strip().lower()
        if region == "cn":
            return "https://openapi.longbridge.cn"
        return None

    async def fetch_today_bar(self, ticker: str) -> dict[str, Any] | None:
        """Fetch today's single daily bar for ``ticker``; returns None if none yet."""
        key = (ticker.upper(), self._today_ny())
        now = time.monotonic()

        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and cached[1] > now:
                log.debug("Longbridge cache hit for %s", ticker)
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
            return self._ensure_ctx().candlesticks(
                symbol,
                Period.Day,
                2,
                AdjustType.NoAdjust,
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

        bar_date = ny_from_ts(ts_ms)
        today = self._today_ny()
        if bar_date != today:
            # Before the open — and on weekends/holidays — the most recent
            # completed daily candle belongs to an earlier session. That
            # bar is already in the local cache, so reporting it here
            # would duplicate the date and leave today's session missing.
            log.info(
                "Longbridge's latest daily bar for %s is %s, not %s; "
                "no bar for the current session yet",
                ticker, bar_date, today,
            )
            return None

        return {
            "t": ts_ms,
            "o": float(last.open),
            "h": float(last.high),
            "l": float(last.low),
            "c": float(last.close),
            "v": float(last.volume or 0),
            "vw": None,  # Longbridge daily bars don't expose per-share VWAP
            "n": None,  # Longbridge daily bars don't expose trade count
        }

    def _format_symbol(self, ticker: str) -> str:
        """Map a US ticker to Longbridge's ``<TICKER>.<REGION>`` form.

        Three cases:

        * Bare US ticker (no dot) → append the configured US region suffix
          (e.g. ``AAPL`` → ``AAPL.US``).
        * US OCC class-share ticker like ``BRK.B`` / ``BRK.A`` / ``BF.B``
          where the trailing segment is a single letter → the dot is part of
          the OCC symbol, not a region; append ``.US`` (e.g. ``BRK.B.US``).
        * Anything else with a dot is assumed to already carry a non-US
          region suffix (e.g. ``700.HK``) and is passed through unchanged.
        """
        upper = ticker.upper()
        region_suffix = self._settings.longbridge_region_suffix.lstrip(".").upper()
        if "." in upper:
            tail = upper.rsplit(".", 1)[-1]
            if tail == region_suffix:
                return upper
            if len(tail) != 1 or not tail.isalpha():
                return upper
        return f"{upper}{self._settings.longbridge_region_suffix}"

    @staticmethod
    def _candle_ts_ms(candle: Any) -> int:
        """Return NY-midnight epoch-ms for ``candle.timestamp``.

        The SDK hands back a **naive** datetime whose wall-clock fields are
        the *session date* in US/Eastern — not a UTC instant. Reading it as
        UTC would shift the bar back a day (00:00 ET is 05:00 UTC, so
        ``2026-11-05 00:00`` would land on ``2026-11-04``), and the bar
        would then be filed under the wrong session.

        A naive timestamp is therefore anchored to ET directly. An
        aware one already carries a real instant, so it is converted.
        """
        ts = candle.timestamp
        if isinstance(ts, datetime):
            if ts.tzinfo is None:
                # Naive: the fields are already the ET session date.
                if NY_TZ is None:  # pragma: no cover - Python <3.9
                    ts = ts.replace(tzinfo=timezone.utc)
                else:
                    ts = ts.replace(tzinfo=NY_TZ)
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