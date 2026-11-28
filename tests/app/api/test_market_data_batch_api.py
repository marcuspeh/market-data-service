"""HTTP-level tests for the POST /market-data/batch endpoint.

The service is replaced by a stub, so no upstream (Polygon / Longbridge)
is contacted — these tests cover the endpoint's contract only.
"""
from __future__ import annotations

from datetime import date

import httpx
import pytest
from fastapi import FastAPI

from app.api import market_data as market_data_api
from app.api.market_data import router
from app.clients.polygon import PolygonError


def _bars_payload(ticker: str, start: date, end: date) -> dict:
    return {
        "ticker": ticker,
        "from": start,
        "to": end,
        "backfilled_bars": 0,
        "bars": [
            {
                "ticker": ticker,
                "timestamp": 1762992000000,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 1000.0,
                "vwap": None,
                "trade_count": None,
                "source": "cache",
            }
        ],
    }


class FakeService:
    """Records the batch calls and replays a canned outcome."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], date, date]] = []
        self.fail: dict[str, Exception] = {}

    async def get_bars_batch(
        self, tickers: list[str], start: date, end: date
    ) -> dict:
        self.calls.append((list(tickers), start, end))
        results = []
        errors = []
        for ticker in tickers:
            exc = self.fail.get(ticker)
            if exc is not None:
                errors.append({"ticker": ticker, "error": exc})
            else:
                results.append(_bars_payload(ticker, start, end))
        return {"results": results, "errors": errors}


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> FakeService:
    fake = FakeService()
    monkeypatch.setattr(market_data_api, "_service", fake)
    return fake


@pytest.fixture
def app() -> FastAPI:
    """A bare app exposing only the market-data router."""
    application = FastAPI()
    application.include_router(router)
    return application


@pytest.fixture
async def client(app: FastAPI):
    """An HTTP client bound to the app in-process."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as http_client:
        yield http_client


async def test_batch_returns_bars_for_every_symbol(
    client: httpx.AsyncClient, service: FakeService
):
    response = await client.post(
        "/market-data/batch",
        json={"symbols": ["AAPL", "MSFT"], "from": "2026-11-01", "to": "2026-11-13"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["errors"] == []
    assert [r["ticker"] for r in body["results"]] == ["AAPL", "MSFT"]
    assert body["results"][0]["bars"][0]["close"] == 100.5
    assert body["results"][0]["from"] == "2026-11-01T00:00:00Z"
    assert body["results"][0]["to"] == "2026-11-13T00:00:00Z"


async def test_batch_isolates_a_failing_symbol(
    client: httpx.AsyncClient, service: FakeService
):
    service.fail["MSFT"] = PolygonError("Polygon HTTP 429 for MSFT")

    response = await client.post(
        "/market-data/batch",
        json={"symbols": ["AAPL", "MSFT"], "from": "2026-11-01", "to": "2026-11-13"},
    )

    assert response.status_code == 200
    body = response.json()
    assert [r["ticker"] for r in body["results"]] == ["AAPL"]
    assert len(body["errors"]) == 1
    assert body["errors"][0]["ticker"] == "MSFT"
    assert body["errors"][0]["status_code"] == 502
    assert "Polygon HTTP 429" in body["errors"][0]["detail"]


async def test_batch_collapses_duplicate_symbols(
    client: httpx.AsyncClient, service: FakeService
):
    response = await client.post(
        "/market-data/batch",
        json={
            "symbols": ["aapl", "AAPL", " MSFT "],
            "from": "2026-11-01",
            "to": "2026-11-13",
        },
    )

    assert response.status_code == 200
    assert service.calls[0][0] == ["AAPL", "MSFT"]
    assert [r["ticker"] for r in response.json()["results"]] == ["AAPL", "MSFT"]


async def test_batch_defaults_to_today(
    client: httpx.AsyncClient, service: FakeService
):
    response = await client.post(
        "/market-data/batch",
        json={"symbols": ["AAPL"], "from": "2026-11-01"},
    )

    assert response.status_code == 200
    _symbols, start, end = service.calls[0]
    assert start == date(2026, 11, 1)
    assert end == market_data_api.Settings.now_ny_date()


async def test_batch_rejects_an_inverted_window(client: httpx.AsyncClient):
    response = await client.post(
        "/market-data/batch",
        json={"symbols": ["AAPL"], "from": "2026-11-13", "to": "2026-11-01"},
    )

    assert response.status_code == 400
    assert "on or after" in response.json()["detail"]


async def test_batch_rejects_an_empty_symbol_list(client: httpx.AsyncClient):
    response = await client.post(
        "/market-data/batch",
        json={"symbols": [], "from": "2026-11-01", "to": "2026-11-13"},
    )

    assert response.status_code == 422


async def test_batch_rejects_a_missing_window(client: httpx.AsyncClient):
    response = await client.post("/market-data/batch", json={"symbols": ["AAPL"]})

    assert response.status_code == 422
