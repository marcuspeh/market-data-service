"""Tests for the market-calendar service.

These use the *real* XNYS calendar: ``exchange_calendars`` is a
deterministic offline rules engine, so it is the ground truth here and
there is nothing to mock. The expected values were checked against the
NYSE's published holiday and early-close calendar.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.services.market_calendar import (
    CalendarDateOutOfRange,
    MarketCalendar,
    STATUS_EARLY_CLOSE,
    STATUS_HOLIDAY,
    STATUS_REGULAR,
    STATUS_WEEKEND,
)


@pytest.fixture(scope="module")
def calendar() -> MarketCalendar:
    """A calendar over the real XNYS exchange."""
    return MarketCalendar(name="XNYS")


class TestRegularSession:
    """A full-length session: 09:30-16:00 ET."""

    def test_regular_session(self, calendar: MarketCalendar):
        result = calendar.describe(date(2026, 11, 13))  # Friday

        assert result["status"] == STATUS_REGULAR
        assert result["is_trading_day"] is True
        assert result["is_holiday"] is False
        assert result["is_early_close"] is False
        assert result["open"] == datetime(
            2026, 11, 13, 9, 30, tzinfo=result["open"].tzinfo
        )
        assert result["close"] == datetime(
            2026, 11, 13, 16, 0, tzinfo=result["close"].tzinfo
        )

    def test_session_times_carry_the_eastern_offset(self, calendar: MarketCalendar):
        result = calendar.describe(date(2026, 11, 13))

        assert result["open"].isoformat() == "2026-11-13T09:30:00-05:00"
        assert result["close"].isoformat() == "2026-11-13T16:00:00-05:00"

    def test_summer_session_uses_the_daylight_offset(
        self, calendar: MarketCalendar
    ):
        """July is EDT (-04:00); the wall time is unchanged."""
        result = calendar.describe(date(2026, 7, 2))

        assert result["status"] == STATUS_REGULAR
        assert result["open"].isoformat() == "2026-07-02T09:30:00-04:00"
        assert result["close"].isoformat() == "2026-07-02T16:00:00-04:00"


class TestEarlyClose:
    """A shortened session still closes early — at 13:00 ET."""

    def test_black_friday(self, calendar: MarketCalendar):
        result = calendar.describe(date(2026, 11, 27))

        assert result["status"] == STATUS_EARLY_CLOSE
        assert result["is_trading_day"] is True
        assert result["is_early_close"] is True
        assert result["is_holiday"] is False
        assert result["close"].isoformat() == "2026-11-27T13:00:00-05:00"

    def test_christmas_eve(self, calendar: MarketCalendar):
        result = calendar.describe(date(2026, 12, 24))

        assert result["status"] == STATUS_EARLY_CLOSE
        assert result["close"].isoformat() == "2026-12-24T13:00:00-05:00"

    def test_session_before_independence_day(self, calendar: MarketCalendar):
        """2026-07-03 is a *holiday* (July 4 is a Saturday), not an early close."""
        result = calendar.describe(date(2026, 7, 3))

        assert result["status"] == STATUS_HOLIDAY
        assert result["is_early_close"] is False

    def test_christmas_eve_is_closed_when_christmas_falls_on_a_saturday(
        self, calendar: MarketCalendar
    ):
        """2027-12-25 is a Saturday, so the exchange is closed all day on the 24th."""
        result = calendar.describe(date(2027, 12, 24))

        assert result["status"] == STATUS_HOLIDAY
        assert result["is_trading_day"] is False
        assert result["is_early_close"] is False
        assert result["open"] is None
        assert result["close"] is None


class TestHoliday:
    """A weekday on which the exchange did not trade."""

    def test_new_years_day(self, calendar: MarketCalendar):
        result = calendar.describe(date(2026, 1, 1))

        assert result["status"] == STATUS_HOLIDAY
        assert result["is_trading_day"] is False
        assert result["is_holiday"] is True
        assert result["is_early_close"] is False
        assert result["open"] is None
        assert result["close"] is None

    def test_thanksgiving(self, calendar: MarketCalendar):
        result = calendar.describe(date(2026, 11, 26))

        assert result["status"] == STATUS_HOLIDAY
        assert result["is_holiday"] is True

    def test_christmas(self, calendar: MarketCalendar):
        result = calendar.describe(date(2026, 12, 25))

        assert result["status"] == STATUS_HOLIDAY
        assert result["is_holiday"] is True

    def test_adhoc_closure(self, calendar: MarketCalendar):
        """2025-01-09 — national day of mourning for Jimmy Carter."""
        result = calendar.describe(date(2025, 1, 9))

        assert result["status"] == STATUS_HOLIDAY
        assert result["is_holiday"] is True

    def test_holiday_observed_off_a_saturday(self, calendar: MarketCalendar):
        """Juneteenth 2022 fell on a Sunday, observed Monday 2022-06-20."""
        result = calendar.describe(date(2022, 6, 20))

        assert result["status"] == STATUS_HOLIDAY
        assert result["is_holiday"] is True

    def test_holiday_observed_off_a_saturday_in_2027(self, calendar: MarketCalendar):
        """Juneteenth 2027 falls on a Saturday, observed Friday 2027-06-18."""
        result = calendar.describe(date(2027, 6, 18))

        assert result["status"] == STATUS_HOLIDAY
        assert result["is_holiday"] is True

    def test_the_saturday_itself_is_a_weekend_not_a_holiday(
        self, calendar: MarketCalendar
    ):
        """The exchange was closed on the Saturday, but not *because* of a holiday."""
        result = calendar.describe(date(2027, 6, 19))

        assert result["status"] == STATUS_WEEKEND
        assert result["is_holiday"] is False
        assert result["is_trading_day"] is False


class TestWeekend:
    """A weekend is a non-trading day, but not a holiday."""

    def test_saturday(self, calendar: MarketCalendar):
        result = calendar.describe(date(2026, 11, 14))

        assert result["status"] == STATUS_WEEKEND
        assert result["is_trading_day"] is False
        assert result["is_holiday"] is False
        assert result["is_early_close"] is False
        assert result["open"] is None
        assert result["close"] is None

    def test_sunday(self, calendar: MarketCalendar):
        result = calendar.describe(date(2026, 11, 15))

        assert result["status"] == STATUS_WEEKEND
        assert result["is_holiday"] is False

    def test_weekend_is_not_reported_as_an_early_close(
        self, calendar: MarketCalendar
    ):
        assert calendar.is_early_close(date(2026, 11, 14)) is False


class TestInvariants:
    """The flag relationships every consumer relies on."""

    @pytest.mark.parametrize(
        "d",
        [
            date(2026, 11, 13),  # regular session
            date(2026, 11, 27),  # early close
            date(2026, 12, 25),  # holiday
            date(2026, 11, 14),  # Saturday
            date(2025, 1, 9),  # adhoc closure
        ],
    )
    def test_is_trading_day_matches_the_status(self, calendar: MarketCalendar, d: date):
        result = calendar.describe(d)

        assert result["is_trading_day"] == (
            result["status"] in (STATUS_REGULAR, STATUS_EARLY_CLOSE)
        )
        assert not result["is_early_close"] or result["is_trading_day"]
        assert (result["open"] is not None) == result["is_trading_day"]
        assert (result["close"] is not None) == result["is_trading_day"]
        assert result["status"] in (
            STATUS_REGULAR,
            STATUS_EARLY_CLOSE,
            STATUS_HOLIDAY,
            STATUS_WEEKEND,
        )


class TestSupportedRange:
    """Out-of-window dates raise rather than answering wrongly."""

    def test_date_before_the_window_raises(self, calendar: MarketCalendar):
        first, _ = calendar.supported_range

        with pytest.raises(CalendarDateOutOfRange) as exc:
            calendar.describe(first - timedelta(days=1))

        assert first.isoformat() in str(exc.value)

    def test_date_after_the_window_raises(self, calendar: MarketCalendar):
        _, last = calendar.supported_range

        with pytest.raises(CalendarDateOutOfRange) as exc:
            calendar.describe(last + timedelta(days=1))

        assert last.isoformat() in str(exc.value)

    def test_the_first_and_last_supported_dates_answer(self, calendar: MarketCalendar):
        """The boundaries themselves are inside the range and must answer."""
        first, last = calendar.supported_range

        assert calendar.is_trading_day(first) is True
        assert calendar.is_trading_day(last) is True

    def test_every_day_inside_the_window_answers(self, calendar: MarketCalendar):
        """No day inside the advertised range may raise.

        This is the regression test for the library's bounds check, which
        compares a date against the first/last *session* rather than
        against the window we asked for.
        """
        first, last = calendar.supported_range
        d = first
        while d <= last:
            assert calendar.is_trading_day(d) in (True, False), d
            d += timedelta(days=1)


class TestCalendarSelection:
    """The exchange calendar is swappable."""

    def test_name_defaults_to_the_setting(self, calendar: MarketCalendar):
        assert calendar.name == "XNYS"

    def test_an_explicit_name_wins(self):
        assert MarketCalendar(name="XTKS").name == "XTKS"
