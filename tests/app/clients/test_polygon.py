"""Tests for the Polygon async client."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.clients.polygon import PolygonClient, PolygonError
from tests.conftest import FakeSettings


@pytest.fixture
def client(fake_settings: FakeSettings) -> PolygonClient:
    return PolygonClient(fake_settings)


def _ok_response(payload: dict) -> httpx.Response:
    request = httpx.Request("GET", "https://api.polygon.io/v2/aggs/ticker/X/range/1/day/a/b")
    return httpx.Response(200, json=payload, request=request)


def _error_response(status_code: int, payload: dict | None = None) -> httpx.Response:
    request = httpx.Request("GET", "https://api.polygon.io/v2/aggs/ticker/X/range/1/day/a/b")
    return httpx.Response(status_code, json=payload or {}, request=request)


class TestUrlConstruction:
    async def test_builds_correct_url_and_params(self, client: PolygonClient):
        captured = {}

        async def fake_get(self, url, *, params=None, headers=None):
            captured["url"] = url
            captured["params"] = params
            captured["headers"] = headers
            return _ok_response({"status": "OK", "results": []})

        with patch.object(httpx.AsyncClient, "get", fake_get):
            await client.fetch_daily_bars(
                "AAPL", date(2026, 7, 1), date(2026, 8, 1)
            )

        assert captured["url"] == (
            "https://api.polygon.io/v2/aggs/ticker/AAPL/range"
            "/1/day/2026-07-01/2026-08-01"
        )
        assert captured["params"] == {
            "adjusted": "true",
            "sort": "asc",
            "limit": 5000,
        }
        assert captured["headers"]["Authorization"] == "Bearer test-key"


class TestResponseHandling:
    async def test_returns_results_on_ok(self, client: PolygonClient):
        payload = {"status": "OK", "results": [{"t": 1, "o": 1}]}
        with patch.object(httpx.AsyncClient, "get", AsyncMock(return_value=_ok_response(payload))):
            result = await client.fetch_daily_bars(
                "AAPL", date(2026, 7, 1), date(2026, 7, 2)
            )
        assert result == [{"t": 1, "o": 1}]

    async def test_returns_empty_list_when_no_results(self, client: PolygonClient):
        payload = {"status": "OK", "results": None}
        with patch.object(httpx.AsyncClient, "get", AsyncMock(return_value=_ok_response(payload))):
            result = await client.fetch_daily_bars(
                "AAPL", date(2026, 7, 1), date(2026, 7, 2)
            )
        assert result == []

    async def test_accepts_delayed_status(self, client: PolygonClient):
        payload = {"status": "DELAYED", "results": [{"t": 1}]}
        with patch.object(httpx.AsyncClient, "get", AsyncMock(return_value=_ok_response(payload))):
            result = await client.fetch_daily_bars(
                "AAPL", date(2026, 7, 1), date(2026, 7, 2)
            )
        assert result == [{"t": 1}]

    async def test_raises_on_error_status(self, client: PolygonClient):
        payload = {"status": "ERROR", "error": "bad ticker"}
        with patch.object(httpx.AsyncClient, "get", AsyncMock(return_value=_ok_response(payload))):
            with pytest.raises(PolygonError, match="bad ticker"):
                await client.fetch_daily_bars(
                    "AAPL", date(2026, 7, 1), date(2026, 7, 2)
                )

    async def test_raises_on_http_error(self, client: PolygonClient):
        with patch.object(
            httpx.AsyncClient,
            "get",
            AsyncMock(return_value=_error_response(401)),
        ):
            with pytest.raises(httpx.HTTPStatusError):
                await client.fetch_daily_bars(
                    "AAPL", date(2026, 7, 1), date(2026, 7, 2)
                )


class TestMissingApiKey:
    async def test_raises_when_api_key_is_empty(self, fake_settings: FakeSettings):
        fake_settings.polygon_api_key = ""
        client = PolygonClient(fake_settings)
        with pytest.raises(PolygonError, match="POLYGON_API_KEY"):
            await client.fetch_daily_bars(
                "AAPL", date(2026, 7, 1), date(2026, 7, 2)
            )