"""Parser for SSGA / SPDR holdings .xlsx files.

SSGA publishes holdings as a real .xlsx workbook. The first few rows are
metadata; the holdings table header starts around row 4 and uses the
columns ``Ticker``, ``Name``, ``Weight`` (case-insensitive, with some variants).
"""
import io
from typing import Any

import pandas as pd

from app.logging_setup import client
from app.services.parsers import is_valid_ticker

log = client()

_REQUIRED_COLUMNS = ("Ticker", "Name", "Weight")


def parse(content: bytes) -> list[dict[str, Any]]:
    """Parse the raw .xlsx bytes and return a list of holdings."""
    df = pd.read_excel(io.BytesIO(content), skiprows=4)
    df.columns = [str(col).strip() for col in df.columns]

    for col in _REQUIRED_COLUMNS:
        if col not in df.columns:
            matches = [c for c in df.columns if col.lower() in c.lower()]
            if matches:
                df.rename(columns={matches[0]: col}, inplace=True)
            else:
                raise ValueError(
                    f"Required column '{col}' not found in SSGA holdings file. "
                    f"Found columns: {df.columns.tolist()}"
                )

    df = df.dropna(subset=["Ticker", "Weight"])

    constituents: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        ticker = str(row["Ticker"]).strip()
        if not is_valid_ticker(ticker):
            continue
        try:
            constituents.append(
                {
                    "ticker": ticker,
                    "name": str(row["Name"]).strip(),
                    "weight": float(row["Weight"]),
                }
            )
        except (ValueError, TypeError):
            continue

    log.info("SSGA parser: extracted %d holdings", len(constituents))
    return constituents