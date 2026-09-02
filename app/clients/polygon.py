import logging
from datetime import date
from typing import Any

import httpx

from app.config.settings import Settings

logger = logging.getLogger(__name__)


class PolygonError(RuntimeError):
    pass


class PolygonClient:
    """Thin async wrapper over Polygon's daily aggregates endpoint."""

    def __init__(self, settings: Settings, *, timeout: float = 15.0) -> None:
        self._settings = settings
        self._timeout = timeout

    async def fetch_daily_bars(
        self,
        ticker: str,
        start: date,
        end: date,
    ) -> list[dict[str, Any]]:
        """Fetch historical daily bars from Polygon; today's bar comes from Longbridge."""
        if not self._settings.polygon_api_key:
            raise PolygonError("POLYGON_API_KEY is not configured")

        url = (
            f"{self._settings.polygon_base_url}/v2/aggs/ticker/{ticker}/range"
            f"/1/day/{start.isoformat()}/{end.isoformat()}"
        )
        params = {"adjusted": "true", "sort": "asc", "limit": 5000}
        headers = {"Authorization": f"Bearer {self._settings.polygon_api_key}"}

        logger.info(f"Polygon GET {url}")
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            payload = resp.json()

        status = payload.get("status")
        if status not in ("OK", "DELAYED"):
            raise PolygonError(
                f"Polygon returned status={status!r} for {ticker}: "
                f"{payload.get('error') or payload.get('message')}"
            )

        return payload.get("results") or []