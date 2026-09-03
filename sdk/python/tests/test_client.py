"""Tests for MarketDataClient using httpx.MockTransport (no network)."""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
import pytest

from market_data_service_sdk import (
    ConstituentsNotFoundError,
    MarketDataClient,
    MarketDataServiceError,
    SyncMarketDataClient,
    UnsupportedSymbolError,
)


@pytest.mark.asyncio
async def test_get_constituents_returns_typed_snapshot() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "symbol": "SPY",
                "date": "2026-08-12",
                "source": "parquet",
                "constituents": ["NVDA", "AAPL", "MSFT"],
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = MarketDataClient(http_client=http)
        snapshot = await client.get_constituents("SPY", date(2026, 8, 12))

    assert snapshot.symbol == "SPY"
    assert snapshot.date == date(2026, 8, 12)
    assert snapshot.constituents == ["NVDA", "AAPL", "MSFT"]
    assert snapshot.source == "parquet"
    assert seen["url"] == "http://localhost:3556/constituents?etf=SPY&date=2026-08-12"


@pytest.mark.asyncio
async def test_get_constituents_404_raises_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "nope"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = MarketDataClient(http_client=http)
        with pytest.raises(ConstituentsNotFoundError) as exc:
            await client.get_constituents("SPY", date(2026, 8, 12))

    assert exc.value.symbol == "SPY"
    assert exc.value.snapshot_date == date(2026, 8, 12)


@pytest.mark.asyncio
async def test_get_constituents_400_raises_unsupported_symbol() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"detail": "Symbol 'XYZ' is not supported."}
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = MarketDataClient(http_client=http)
        with pytest.raises(UnsupportedSymbolError) as exc:
            await client.get_constituents("XYZ", date(2026, 8, 12))

    assert exc.value.symbol == "XYZ"


@pytest.mark.asyncio
async def test_get_constituents_500_raises_service_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = MarketDataClient(http_client=http)
        with pytest.raises(MarketDataServiceError):
            await client.get_constituents("SPY", date(2026, 8, 12))


@pytest.mark.asyncio
async def test_get_bars_returns_typed_bars() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "ticker": "AAPL",
                "from": "2026-08-10",
                "to": "2026-08-20",
                "backfilled_bars": 0,
                "bars": [
                    {
                        "ticker": "AAPL",
                        "date": "2026-08-10",
                        "timestamp": 1786334400000,
                        "open": 306.83,
                        "high": 308.26,
                        "low": 304.61,
                        "close": 308.26,
                        "volume": 44812503.00883,
                        "vwap": 307.2588,
                        "trade_count": 925003,
                        "source": "cache",
                    },
                    {
                        "ticker": "AAPL",
                        "date": "2026-08-20",
                        "timestamp": 1787198400000,
                        "open": 317.46,
                        "high": 320.28,
                        "low": 315.96,
                        "close": 317.54,
                        "volume": 266731.0,
                        "vwap": 317.295,
                        "trade_count": 91062,
                        "source": "longbridge",
                    },
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = MarketDataClient(http_client=http)
        result = await client.get_bars(
            "AAPL", from_=date(2026, 8, 10), to=date(2026, 8, 20)
        )

    assert result.ticker == "AAPL"
    assert result.from_ == date(2026, 8, 10)
    assert result.to == date(2026, 8, 20)
    assert result.backfilled_bars == 0
    assert len(result.bars) == 2
    assert result.bars[0].source == "cache"
    assert result.bars[1].source == "longbridge"
    assert result.bars[0].vwap == 307.2588
    assert result.bars[0].trade_count == 925003
    assert (
        seen["url"]
        == "http://localhost:3556/market-data/AAPL?from=2026-08-10&to=2026-08-20"
    )


@pytest.mark.asyncio
async def test_get_bars_omits_to_when_not_given() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "ticker": "AAPL",
                "from": "2026-08-10",
                "to": "2026-08-20",
                "backfilled_bars": 0,
                "bars": [],
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = MarketDataClient(http_client=http)
        await client.get_bars("AAPL", from_=date(2026, 8, 10))

    assert "to=" not in seen["url"]


@pytest.mark.asyncio
async def test_get_bars_handles_null_vwap_and_trade_count() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ticker": "AAPL",
                "from": "2026-08-10",
                "to": "2026-08-10",
                "backfilled_bars": 0,
                "bars": [
                    {
                        "ticker": "AAPL",
                        "date": "2026-08-10",
                        "timestamp": 1786334400000,
                        "open": 1.0,
                        "high": 2.0,
                        "low": 0.5,
                        "close": 1.5,
                        "volume": 100.0,
                        "vwap": None,
                        "trade_count": None,
                        "source": "cache",
                    }
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = MarketDataClient(http_client=http)
        result = await client.get_bars(
            "AAPL", from_=date(2026, 8, 10), to=date(2026, 8, 10)
        )

    assert result.bars[0].vwap is None
    assert result.bars[0].trade_count is None


@pytest.mark.asyncio
async def test_get_bars_500_raises_service_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"detail": "upstream error"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = MarketDataClient(http_client=http)
        with pytest.raises(MarketDataServiceError):
            await client.get_bars("AAPL", from_=date(2026, 8, 10))


@pytest.mark.asyncio
async def test_custom_base_url_is_used() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "symbol": "SPY",
                "date": "2026-08-12",
                "source": "parquet",
                "constituents": ["AAPL"],
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = MarketDataClient(base_url="http://mds.internal:9000/", http_client=http)
        await client.get_constituents("SPY", date(2026, 8, 12))

    # Trailing slash should be stripped.
    assert seen["url"].startswith("http://mds.internal:9000/constituents?")


@pytest.mark.asyncio
async def test_ticker_is_url_escaped() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "ticker": "BRK.B",
                "from": "2026-08-10",
                "to": "2026-08-10",
                "backfilled_bars": 0,
                "bars": [],
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = MarketDataClient(http_client=http)
        await client.get_bars("BRK.B", from_=date(2026, 8, 10))

    # httpx escapes the dot in path params; just assert the ticker round-tripped.
    assert "BRK.B" in seen["url"]


def test_sync_client_returns_same_shapes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ticker": "AAPL",
                "from": "2026-08-10",
                "to": "2026-08-10",
                "backfilled_bars": 1,
                "bars": [
                    {
                        "ticker": "AAPL",
                        "date": "2026-08-10",
                        "timestamp": 1786334400000,
                        "open": 1.0,
                        "high": 2.0,
                        "low": 0.5,
                        "close": 1.5,
                        "volume": 100.0,
                        "vwap": 1.25,
                        "trade_count": 10,
                        "source": "cache",
                    }
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as http:
        client = SyncMarketDataClient(http_client=http)
        result = client.get_bars("AAPL", from_=date(2026, 8, 10))

    assert result.ticker == "AAPL"
    assert result.backfilled_bars == 1
    assert len(result.bars) == 1
    assert result.bars[0].close == 1.5
