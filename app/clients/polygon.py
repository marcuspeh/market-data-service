from datetime import date
from typing import Any

import httpx

from app.config.settings import Settings
from app.logging_setup import client

log = client()


class PolygonError(RuntimeError):
    pass


class PolygonClient:
    """Thin async wrapper over Polygon's reference + aggregate endpoints.

    Daily bars are always requested **raw** (``adjusted=false``): the
    parquet cache stores vendor prices verbatim and the adjusted view is
    rebuilt on read from the split/dividend records fetched here. That
    keeps stored numbers stable and makes reconciliations deterministic.
    """

    def __init__(self, settings: Settings, *, timeout: float = 15.0) -> None:
        self._settings = settings
        self._timeout = timeout

    async def fetch_daily_bars(
        self,
        ticker: str,
        start: date,
        end: date,
    ) -> list[dict[str, Any]]:
        """Fetch unadjusted (raw) daily bars; today's bar comes from Longbridge."""
        url = (
            f"{self._settings.polygon_base_url}/v2/aggs/ticker/{ticker}/range"
            f"/1/day/{start.isoformat()}/{end.isoformat()}"
        )
        params: dict[str, Any] = {
            "adjusted": "false",
            "sort": "asc",
            "limit": 5000,
        }
        return await self._fetch_all(url, params, ticker)

    async def fetch_splits(
        self,
        ticker: str,
        start: date,
        end: date,
    ) -> list[dict[str, Any]]:
        """Split records with ``start <= execution_date <= end``."""
        params: dict[str, Any] = {
            "ticker": ticker.upper(),
            "execution_date.gte": start.isoformat(),
            "execution_date.lte": end.isoformat(),
            "sort": "execution_date",
            "order": "asc",
            "limit": 1000,
        }
        return await self._fetch_all(
            f"{self._settings.polygon_base_url}/stocks/v1/splits", params, ticker
        )

    async def fetch_dividends(
        self,
        ticker: str,
        start: date,
        end: date,
    ) -> list[dict[str, Any]]:
        """Dividend records with ``start <= ex_dividend_date <= end``."""
        params: dict[str, Any] = {
            "ticker": ticker.upper(),
            "ex_dividend_date.gte": start.isoformat(),
            "ex_dividend_date.lte": end.isoformat(),
            "sort": "ex_dividend_date",
            "order": "asc",
            "limit": 1000,
        }
        return await self._fetch_all(
            f"{self._settings.polygon_base_url}/stocks/v1/dividends", params, ticker
        )

    async def _fetch_all(
        self,
        url: str,
        params: dict[str, Any] | None,
        ticker: str,
    ) -> list[dict[str, Any]]:
        """Page through a Polygon endpoint, concatenating ``results``."""
        if not self._settings.polygon_api_key:
            raise PolygonError("POLYGON_API_KEY is not configured")

        headers = {"Authorization": f"Bearer {self._settings.polygon_api_key}"}
        results: list[dict[str, Any]] = []
        # Guards against a server that keeps handing back the same cursor.
        seen_urls: set[str] = set()

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            while url:
                if url in seen_urls:
                    raise PolygonError(
                        f"Polygon pagination loop detected for {ticker}: "
                        f"{url} was already fetched"
                    )
                seen_urls.add(url)

                log.info("Polygon GET %s", url)
                try:
                    resp = await client.get(url, params=params, headers=headers)
                    resp.raise_for_status()
                except httpx.HTTPStatusError as e:
                    raise PolygonError(
                        f"Polygon HTTP {e.response.status_code} for {ticker}: "
                        f"{e.response.text}"
                    ) from e
                except httpx.HTTPError as e:
                    raise PolygonError(
                        f"Polygon request for {ticker} failed: {e}"
                    ) from e
                payload = resp.json()

                status = payload.get("status")
                if status not in ("OK", "DELAYED"):
                    raise PolygonError(
                        f"Polygon returned status={status!r} for {ticker}: "
                        f"{payload.get('error') or payload.get('message')}"
                    )

                results.extend(payload.get("results") or [])

                next_url = payload.get("next_url")
                if next_url:
                    # The next page already carries its own query string
                    # (including the ``cursor``), so params must not be
                    # resent. ``params=None`` — not ``{}`` — because httpx
                    # *replaces* the query string when given an empty dict,
                    # which would drop the cursor and make Polygon serve
                    # page 1 again forever.
                    url = next_url
                    params = None
                else:
                    url = ""

        return results
