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
            "adjusted": "false",
            "sort": "asc",
            "limit": 5000,
        }
        assert captured["headers"]["Authorization"] == "Bearer test-key"

    async def test_builds_split_reference_url_and_params(
        self, client: PolygonClient
    ):
        captured = {}

        async def fake_get(self, url, *, params=None, headers=None):
            captured["url"] = url
            captured["params"] = params
            return _ok_response({"status": "OK", "results": []})

        with patch.object(httpx.AsyncClient, "get", fake_get):
            await client.fetch_splits(
                "aapl", date(2026, 7, 1), date(2026, 8, 1)
            )

        assert captured["url"] == "https://api.polygon.io/stocks/v1/splits"
        assert captured["params"] == {
            "ticker": "AAPL",
            "execution_date.gte": "2026-07-01",
            "execution_date.lte": "2026-08-01",
            "sort": "execution_date",
            "order": "asc",
            "limit": 1000,
        }

    async def test_builds_dividend_reference_url_and_params(
        self, client: PolygonClient
    ):
        captured = {}

        async def fake_get(self, url, *, params=None, headers=None):
            captured["url"] = url
            captured["params"] = params
            return _ok_response({"status": "OK", "results": []})

        with patch.object(httpx.AsyncClient, "get", fake_get):
            await client.fetch_dividends(
                "aapl", date(2026, 7, 1), date(2026, 8, 1)
            )

        assert captured["url"] == "https://api.polygon.io/stocks/v1/dividends"
        assert captured["params"] == {
            "ticker": "AAPL",
            "ex_dividend_date.gte": "2026-07-01",
            "ex_dividend_date.lte": "2026-08-01",
            "sort": "ex_dividend_date",
            "order": "asc",
            "limit": 1000,
        }


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


class TestPagination:
    async def test_follows_next_url_and_concatenates_results(
        self, client: PolygonClient
    ):
        page2 = "https://api.polygon.io/v2/aggs/ticker/AAPL/range/1/day/x/y?cursor=2"
        captured = []

        async def fake_get(self, url, *, params=None, headers=None):
            captured.append((url, params))
            if len(captured) == 1:
                return _ok_response(
                    {
                        "status": "OK",
                        "results": [{"t": 1, "c": 1.0}],
                        "next_url": page2,
                    }
                )
            return _ok_response({"status": "OK", "results": [{"t": 2, "c": 2.0}]})

        with patch.object(httpx.AsyncClient, "get", fake_get):
            result = await client.fetch_daily_bars(
                "AAPL", date(2026, 7, 1), date(2026, 7, 2)
            )

        assert result == [{"t": 1, "c": 1.0}, {"t": 2, "c": 2.0}]
        assert captured[0][1]["adjusted"] == "false"
        # The second page carries its own query string — params are dropped.
        assert captured[1][0] == page2
        assert captured[1][1] == {}

    async def test_stops_when_next_url_is_absent(self, client: PolygonClient):
        with patch.object(
            httpx.AsyncClient,
            "get",
            AsyncMock(return_value=_ok_response({"status": "OK", "results": [{"t": 1}]})),
        ) as mock_get:
            await client.fetch_daily_bars(
                "AAPL", date(2026, 7, 1), date(2026, 7, 2)
            )
        assert mock_get.call_count == 1


class TestMissingApiKey:
    async def test_raises_when_api_key_is_empty(self, fake_settings: FakeSettings):
        fake_settings.polygon_api_key = ""
        client = PolygonClient(fake_settings)
        with pytest.raises(PolygonError, match="POLYGON_API_KEY"):
            await client.fetch_daily_bars(
                "AAPL", date(2026, 7, 1), date(2026, 7, 2)
            )