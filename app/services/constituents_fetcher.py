"""Async fetcher that delegates parsing to a per-provider parser.

The registry below maps each supported ticker to ``(provider, link)``. The
provider key selects which parser module is invoked on the downloaded bytes.
"""
import logging
from typing import Any

import httpx

from app.services.parsers import ishares, ssga

logger = logging.getLogger(__name__)

# Each provider must expose ``parse(content: bytes) -> list[dict]``.
PROVIDER_PARSERS: dict[str, Any] = {
    "ssga": ssga,
    "ishares": ishares,
}

ETF_REGISTRY: dict[str, dict[str, str]] = {
    # SPDR S&P 500 — State Street Global Advisors
    "SPY": {
        "provider": "ssga",
        "link": (
            "https://www.ssga.com/us/en/intermediary/library-content/"
            "products/fund-data/etfs/us/holdings-daily-us-en-spy.xlsx"
        ),
    },
    # QQQ is provided via iShares Nasdaq-100 ETF (US product 351653) — same
    # constituents, but iShares exposes a stable CSV URL while Invesco's
    # per-product holdings .xlsx slug is not stable.
    "QQQ": {
        "provider": "ishares",
        "link": (
            "https://www.ishares.com/us/products/351653/"
            "ishares-nasdaq-100-etf/latest-holdings.csv"
        ),
    },
    "IWM": {
        "provider": "ishares",
        "link": (
            "https://www.ishares.com/us/products/239710/"
            "ishares-russell-2000-etf/latest-holdings.csv"
        ),
    },
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    )
}


class UnsupportedSymbolError(ValueError):
    """Raised when the requested symbol is not in the registry."""


def get_entry(symbol: str) -> dict[str, str]:
    """Return the registry entry for a symbol, or raise."""
    symbol = symbol.upper()
    try:
        return ETF_REGISTRY[symbol]
    except KeyError:
        raise UnsupportedSymbolError(
            f"Symbol '{symbol}' is not in the ETF registry. "
            f"Supported: {sorted(ETF_REGISTRY)}"
        )


async def fetch_etf_constituents(symbol: str) -> list[dict[str, Any]]:
    """Fetch the holdings of any ETF in the registry, dispatching to the
    parser registered for its provider."""
    entry = get_entry(symbol)
    provider = entry["provider"]
    url = entry["link"]

    parser = PROVIDER_PARSERS[provider]

    logger.info(f"Fetching {symbol.upper()} (provider={provider}) from {url}")
    async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        return parser.parse(response.content)