import io
import logging
from typing import Any

import httpx
import pandas as pd

logger = logging.getLogger(__name__)

SPY_URL = (
    "https://www.ssga.com/us/en/intermediary/library-content/products/fund-data/"
    "etfs/us/holdings-daily-us-en-spy.xlsx"
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    )
}


def _parse_excel(content: bytes) -> list[dict[str, Any]]:
    # SSGA Excel files typically have metadata in the first few rows.
    # The actual table header starts around row 4 or 5.
    df = pd.read_excel(io.BytesIO(content), skiprows=4)

    # Clean up column names (strip whitespace)
    df.columns = [str(col).strip() for col in df.columns]

    required_cols = ["Ticker", "Name", "Weight"]
    for col in required_cols:
        if col not in df.columns:
            # Fallback: case-insensitive / slight variations
            matches = [c for c in df.columns if col.lower() in c.lower()]
            if matches:
                df.rename(columns={matches[0]: col}, inplace=True)
            else:
                raise ValueError(
                    f"Required column '{col}' not found in Excel file. "
                    f"Found: {df.columns.tolist()}"
                )

    # Filter out empty rows or footer rows
    df = df.dropna(subset=["Ticker", "Weight"])

    constituents: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        try:
            constituents.append(
                {
                    "ticker": str(row["Ticker"]).strip(),
                    "name": str(row["Name"]).strip(),
                    "weight": float(row["Weight"]),
                }
            )
        except (ValueError, TypeError):
            continue

    return constituents


async def fetch_spy_constituents() -> list[dict[str, Any]]:
    logger.info(f"Fetching SPY constituents from {SPY_URL}")
    async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True) as client:
        response = await client.get(SPY_URL)
        response.raise_for_status()
        return _parse_excel(response.content)