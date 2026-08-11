"""Parser for SSGA / SPDR holdings .xlsx files.

SSGA publishes holdings as a real .xlsx workbook. The first few rows contain
metadata; the holdings table header starts around row 4 or 5 and uses the
columns ``Ticker``, ``Name``, ``Weight`` (case-insensitive, with some variants).
"""
import io
import logging
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

_REQUIRED_COLUMNS = ("Ticker", "Name", "Weight")


def parse(content: bytes) -> list[dict[str, Any]]:
    """Parse the raw .xlsx bytes and return a list of holdings."""
    df = pd.read_excel(io.BytesIO(content), skiprows=4)
    df.columns = [str(col).strip() for col in df.columns]

    for col in _REQUIRED_COLUMNS:
        if col not in df.columns:
            # Fallback: case-insensitive / slight variation match
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

    logger.info(f"SSGA parser: extracted {len(constituents)} holdings")
    return constituents