"""Unit tests for the pure-Python helpers in MarketBarRepository.

We deliberately skip the async methods that hit the DB — those would
need a test database. The helpers here are pure and exercise the
date/timestamp conversions that the SQL filter relies on.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from app.database.repositories.market_bar import _date_to_ts


class TestDateToTs:
    def test_utc_midnight(self):
        ts = _date_to_ts(date(2026, 8, 11))
        expected = int(
            datetime(2026, 8, 11, tzinfo=timezone.utc).timestamp() * 1000
        )
        assert ts == expected

    def test_in_round_trip(self):
        """date → ts → datetime → date is stable."""
        d = date(2024, 2, 29)  # leap day, just to be safe
        ts = _date_to_ts(d)
        round_tripped = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date()
        assert round_tripped == d

    def test_returns_milliseconds(self):
        # 2026-01-01 00:00 UTC has a known epoch-seconds value; verify the
        # result is that many milliseconds.
        ts = _date_to_ts(date(2026, 1, 1))
        assert ts == 1767225600000