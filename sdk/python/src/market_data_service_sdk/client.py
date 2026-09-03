"""Async + sync HTTP client for the market-data-service proxy."""
from __future__ import annotations

import asyncio
from datetime import date
from typing import Optional
from urllib.parse import quote

import httpx

from .models import Bar, BarsResponse, ConstituentsResponse


class MarketDataServiceError(Exception):
    """Base error for the SDK."""


class ConstituentsNotFoundError(MarketDataServiceError):
    """Raised when the proxy reports no snapshot for ``(symbol, date)``."""

    def __init__(self, symbol: str, snapshot_date: date) -> None:
        super().__init__(
            f"no constituents snapshot for {symbol} on {snapshot_date.isoformat()}"
        )
        self.symbol = symbol
        self.snapshot_date = snapshot_date


class UnsupportedSymbolError(MarketDataServiceError):
    """Raised when the proxy rejects the requested ETF symbol."""

    def __init__(self, symbol: str, detail: str) -> None:
        super().__init__(detail)
        self.symbol = symbol


_DEFAULT_BASE_URL = "http://localhost:3556"
_DEFAULT_TIMEOUT = 10.0


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _build_bar(payload: dict) -> Bar:
    return Bar(
        ticker=payload["ticker"],
        date=_parse_date(payload["date"]),
        timestamp=int(payload["timestamp"]),
        open=float(payload["open"]),
        high=float(payload["high"]),
        low=float(payload["low"]),
        close=float(payload["close"]),
        volume=float(payload["volume"]),
        vwap=(float(payload["vwap"]) if payload.get("vwap") is not None else None),
        trade_count=(
            int(payload["trade_count"]) if payload.get("trade_count") is not None else None
        ),
        source=str(payload.get("source", "")),
    )


class _BaseClient:
    def __init__(
        self,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def _raise_for_status(self, response: httpx.Response, url: str) -> None:
        if response.status_code >= 400:
            detail = ""
            try:
                body = response.json()
                if isinstance(body, dict):
                    detail = str(body.get("detail", ""))
            except Exception:
                detail = response.text
            raise MarketDataServiceError(
                f"market-data-service returned status {response.status_code} "
                f"for {url}: {detail or response.text}"
            )


class MarketDataClient(_BaseClient):
    """Async client for the market-data-service proxy."""

    def __init__(
        self,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: float = _DEFAULT_TIMEOUT,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        super().__init__(base_url, timeout)
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self) -> "MarketDataClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def get_constituents(
        self, symbol: str, snapshot_date: date
    ) -> ConstituentsResponse:
        symbol = symbol.upper()
        url = f"{self._base_url}/constituents"
        params = {"etf": symbol, "date": snapshot_date.isoformat()}

        response = await self._http.get(url, params=params)

        if response.status_code == 404:
            raise ConstituentsNotFoundError(symbol, snapshot_date)
        if response.status_code == 400:
            detail = ""
            try:
                body = response.json()
                if isinstance(body, dict):
                    detail = str(body.get("detail", ""))
            except Exception:
                detail = response.text
            raise UnsupportedSymbolError(symbol, detail or "unsupported symbol")
        self._raise_for_status(response, url)

        payload = response.json()
        return ConstituentsResponse(
            symbol=str(payload["symbol"]),
            date=_parse_date(payload["date"]),
            constituents=list(payload["constituents"]),
            source=str(payload.get("source", "")),
        )

    async def get_bars(
        self,
        ticker: str,
        from_: date,
        to: Optional[date] = None,
    ) -> BarsResponse:
        ticker = ticker.upper()
        params: dict[str, str] = {"from": from_.isoformat()}
        if to is not None:
            params["to"] = to.isoformat()

        url = f"{self._base_url}/market-data/{quote(ticker, safe='')}"
        response = await self._http.get(url, params=params)
        self._raise_for_status(response, url)

        payload = response.json()
        bars = [_build_bar(b) for b in payload.get("bars", [])]
        return BarsResponse(
            ticker=str(payload["ticker"]),
            from_=_parse_date(payload["from"]),
            to=_parse_date(payload["to"]),
            backfilled_bars=int(payload.get("backfilled_bars", 0)),
            bars=bars,
        )


class SyncMarketDataClient(_BaseClient):
    """Synchronous wrapper around the async client."""

    def __init__(
        self,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: float = _DEFAULT_TIMEOUT,
        *,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        super().__init__(base_url, timeout)
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(timeout=timeout)
        self._loop = asyncio.new_event_loop()

    def __enter__(self) -> "SyncMarketDataClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        try:
            if self._owns_client:
                self._http.close()
        finally:
            self._loop.close()

    def get_constituents(
        self, symbol: str, snapshot_date: date
    ) -> ConstituentsResponse:
        symbol = symbol.upper()
        url = f"{self._base_url}/constituents"
        params = {"etf": symbol, "date": snapshot_date.isoformat()}

        response = self._http.get(url, params=params)

        if response.status_code == 404:
            raise ConstituentsNotFoundError(symbol, snapshot_date)
        if response.status_code == 400:
            detail = ""
            try:
                body = response.json()
                if isinstance(body, dict):
                    detail = str(body.get("detail", ""))
            except Exception:
                detail = response.text
            raise UnsupportedSymbolError(symbol, detail or "unsupported symbol")
        self._raise_for_status(response, url)

        payload = response.json()
        return ConstituentsResponse(
            symbol=str(payload["symbol"]),
            date=_parse_date(payload["date"]),
            constituents=list(payload["constituents"]),
            source=str(payload.get("source", "")),
        )

    def get_bars(
        self,
        ticker: str,
        from_: date,
        to: Optional[date] = None,
    ) -> BarsResponse:
        ticker = ticker.upper()
        params: dict[str, str] = {"from": from_.isoformat()}
        if to is not None:
            params["to"] = to.isoformat()

        url = f"{self._base_url}/market-data/{quote(ticker, safe='')}"
        response = self._http.get(url, params=params)
        self._raise_for_status(response, url)

        payload = response.json()
        bars = [_build_bar(b) for b in payload.get("bars", [])]
        return BarsResponse(
            ticker=str(payload["ticker"]),
            from_=_parse_date(payload["from"]),
            to=_parse_date(payload["to"]),
            backfilled_bars=int(payload.get("backfilled_bars", 0)),
            bars=bars,
        )
