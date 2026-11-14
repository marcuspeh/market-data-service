"""HTTP-level tests for the /market-calendar endpoint.

The app is built from the router alone — ``app.main`` is deliberately
not imported, because it calls ``setup_logging()`` at module scope and
would start the APScheduler jobs in its lifespan.
"""
from __future__ import annotations

from datetime import date

import httpx
import pytest
from fastapi import FastAPI

from app.api.market_calendar import router


@pytest.fixture
def app() -> FastAPI:
    """A bare app exposing only the market-calendar router."""
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


async def test_regular_session(client: httpx.AsyncClient):
    response = await client.get("/market-calendar", params={"date": "2026-11-13"})

    assert response.status_code == 200
    assert response.json() == {
        "date": "2026-11-13T00:00:00Z",
        "status": "regular",
        "is_trading_day": True,
        "is_holiday": False,
        "is_early_close": False,
        "open": "2026-11-13T09:30:00-05:00",
        "close": "2026-11-13T16:00:00-05:00",
    }


async def test_early_close(client: httpx.AsyncClient):
    response = await client.get("/market-calendar", params={"date": "2026-11-27"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "early_close"
    assert body["is_early_close"] is True
    assert body["is_trading_day"] is True
    assert body["close"] == "2026-11-27T13:00:00-05:00"


async def test_holiday(client: httpx.AsyncClient):
    response = await client.get("/market-calendar", params={"date": "2026-12-25"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "holiday"
    assert body["is_holiday"] is True
    assert body["is_trading_day"] is False
    assert body["open"] is None
    assert body["close"] is None


async def test_weekend(client: httpx.AsyncClient):
    response = await client.get("/market-calendar", params={"date": "2026-11-14"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "weekend"
    assert body["is_holiday"] is False
    assert body["is_trading_day"] is False


async def test_date_is_a_calendar_day_marker(client: httpx.AsyncClient):
    """The date is the same string /market-data emits for the same day."""
    response = await client.get("/market-calendar", params={"date": "2026-11-13"})

    assert response.json()["date"] == "2026-11-13T00:00:00Z"


async def test_session_times_carry_the_eastern_offset(client: httpx.AsyncClient):
    response = await client.get("/market-calendar", params={"date": "2026-07-02"})

    body = response.json()
    assert body["open"] == "2026-07-02T09:30:00-04:00"
    assert body["close"] == "2026-07-02T16:00:00-04:00"
    assert not body["open"].endswith("Z")


async def test_date_outside_the_supported_range_is_a_400(client: httpx.AsyncClient):
    response = await client.get("/market-calendar", params={"date": "1990-01-02"})

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "outside the supported" in detail
    assert "XNYS" in detail


async def test_malformed_date_is_a_422(client: httpx.AsyncClient):
    response = await client.get("/market-calendar", params={"date": "not-a-date"})

    assert response.status_code == 422


async def test_missing_date_is_a_422(client: httpx.AsyncClient):
    response = await client.get("/market-calendar")

    assert response.status_code == 422
