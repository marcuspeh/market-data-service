"""Parser for iShares ETF holdings CSV files.

iShares publishes holdings at a stable per-product URL
(``/us/products/<id>/<slug>/latest-holdings.csv``). The CSV starts with
several metadata rows (fund name, holdings-as-of date, inception, etc.)
then a header row with columns ``Ticker``, ``Name``, ``Sector``,
``Asset Class``, ``Market Value``, ``Weight (%)``, ... — the relevant
columns are ``Ticker``, ``Name``, and ``Weight (%)``.
"""
import csv
import io
import logging
from typing import Any

logger = logging.getLogger(__name__)


def parse(content: bytes) -> list[dict[str, Any]]:
    """Parse the CSV bytes and return a list of holdings."""
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))

    header: list[str] | None = None
    for row in reader:
        if not row:
            continue
        if row[0].strip().lower() == "ticker":
            header = [col.strip() for col in row]
            break

    if header is None:
        raise ValueError("Could not locate holdings header row in iShares CSV")

    try:
        ticker_idx = header.index("Ticker")
        name_idx = header.index("Name")
        weight_idx = header.index("Weight (%)")
    except ValueError as e:
        raise ValueError(
            f"iShares CSV missing required column: {e}. "
            f"Found headers: {header}"
        ) from e

    constituents: list[dict[str, Any]] = []
    for row in reader:
        if not row or len(row) <= max(ticker_idx, name_idx, weight_idx):
            continue

        ticker = row[ticker_idx].strip()
        name = row[name_idx].strip()
        weight_raw = row[weight_idx].strip()

        if not ticker or ticker in {"-", "—"}:
            continue

        try:
            weight = float(weight_raw.rstrip("%").replace(",", ""))
        except (ValueError, TypeError):
            continue

        constituents.append(
            {"ticker": ticker, "name": name, "weight": weight}
        )

    logger.info(f"iShares parser: extracted {len(constituents)} holdings")
    return constituents