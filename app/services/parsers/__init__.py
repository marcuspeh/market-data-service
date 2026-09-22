"""Shared helpers for ETF holdings parsers."""

from __future__ import annotations

import re

# A valid exchange ticker:
#   - starts with an uppercase letter
#   - contains only letters, digits, dot, dash
#   - 1..6 characters
# Rejects SSGA placeholder codes like "2602335D", "2682320D" (digits + trailing D),
# cash rows ("-", "—"), and blanks.
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,5}$")


def is_valid_ticker(value: object) -> bool:
    """Return True if *value* looks like a real exchange ticker."""
    if value is None:
        return False
    ticker = str(value).strip().upper()
    if not ticker:
        return False
    if ticker in {"-", "—", "N/A", "NA"}:
        return False
    return bool(_TICKER_RE.match(ticker))