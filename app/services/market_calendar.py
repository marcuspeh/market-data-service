"""US market session calendar — holidays, early closes and session hours.

The service needs to answer "did the exchange trade on this date?" for
several reasons that used to be answered by guesswork: the interior-gap
detection in :mod:`app.services.market_data_service` guesses from a
calendar-day span, and downstream consumers keep their own hand-typed copy
of the holiday list.

This module wraps :mod:`exchange_calendars` (the ``XNYS`` calendar) and
answers for a single calendar date:

* whether the exchange held a session (``is_trading_day``),
* whether the date was a market holiday (``is_holiday``),
* whether the session was shortened (``is_early_close``),
* and when the session opened and closed.

The library is a pure, offline, deterministic rules engine — no network
call, no API key — so the calendar is built in-process rather than fetched
and cached like the bar data.

Date semantics
--------------
All dates are **US/Eastern calendar dates**, matching the rest of the
service (``app.config.settings``). The session ``open`` / ``close`` are
instants on that wall clock: a regular session is 09:30-16:00 ET and an
early close is 13:00 ET, but the UTC offset varies with daylight saving
(``-04:00`` in summer, ``-05:00`` in winter).

A holiday is a **weekday on which the exchange did not trade**. A weekend
is not a holiday: both are non-trading days, but the NYSE always observes
a holiday on a weekday (the "nearest weekday" rules move a Saturday
holiday to the preceding Friday), so "not a session and a weekday" is
exactly the holiday set.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from exchange_calendars import get_calendar
from exchange_calendars.errors import DateOutOfBounds

from app.config.settings import get_settings
from app.logging_setup import client

log = client()

#: How many years back / forward the calendar covers. The library's own
#: default window is [today - 20y, today + 1y]; the forward bound is far
#: too tight for a calendar service, because the NYSE publishes the
#: holiday schedule years ahead of time.
BACK_YEARS = 25
FORWARD_YEARS = 5

#: Days of slack added to the *built* window. ``exchange_calendars``
#: decides whether a date is out of bounds by comparing it against the
#: first and last **session** inside the window, not against the
#: [start, end] we asked for. Without slack, a ``start`` that lands on a
#: weekend would make that weekend itself "before the first session" and
#: raise, even though it is inside the advertised range. Two weeks of
#: slack keeps the answerable range tight around the intended window.
BOUND_PADDING_DAYS = 14

#: Session status values.
STATUS_REGULAR = "regular"
STATUS_EARLY_CLOSE = "early_close"
STATUS_HOLIDAY = "holiday"
STATUS_WEEKEND = "weekend"


class CalendarDateOutOfRange(ValueError):
    """The requested date is outside the calendar's supported window."""


def _shift_years(d: date, years: int) -> date:
    """Move ``d`` by whole years, clamping Feb 29 to Feb 28.

    ``date.replace`` raises ``ValueError`` when the target year is not a
    leap year, which would turn every 29 February into an error.
    """
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        return d.replace(year=d.year + years, day=28)


class MarketCalendar:
    """Answers market-session questions for a single calendar date.

    The underlying exchange calendar is built lazily on first use and then
    cached for the life of the process: constructing it is not free, and
    the window only moves with wall-clock time.
    """

    def __init__(self, name: str | None = None) -> None:
        """Create a calendar.

        ``name`` overrides the configured exchange (see
        ``Settings.market_calendar_name``). When omitted the setting is
        read *lazily* rather than at construction time, so importing this
        module never touches the environment.
        """
        self._name = name
        self._calendar: Any = None
        self._early_close_dates: frozenset[date] = frozenset()

    @property
    def name(self) -> str:
        """The exchange calendar's name."""
        if self._name is not None:
            return self._name
        return get_settings().market_calendar_name

    @property
    def calendar(self) -> Any:
        """The underlying ``ExchangeCalendar``, built on first access."""
        if self._calendar is None:
            self._calendar = self._build()
            # ``early_closes`` holds session labels; convert once so the
            # membership test is plain Python rather than a pandas
            # index lookup.
            self._early_close_dates = frozenset(
                ts.date() for ts in self._calendar.early_closes
            )
            log.info(
                "Market calendar %s ready: %s .. %s (%d sessions)",
                self.name,
                self._calendar.first_session.date(),
                self._calendar.last_session.date(),
                len(self._calendar.sessions),
            )
        return self._calendar

    def _build(self) -> Any:
        """Construct the exchange calendar over the supported window."""
        today = get_settings().now_ny_date()
        start = _shift_years(today, -BACK_YEARS) - timedelta(
            days=BOUND_PADDING_DAYS
        )
        end = _shift_years(today, FORWARD_YEARS) + timedelta(
            days=BOUND_PADDING_DAYS
        )
        return get_calendar(self.name, start=start, end=end)

    @property
    def supported_range(self) -> tuple[date, date]:
        """The first and last dates this calendar can answer for.

        These are the first and last *sessions* in the built window, which
        is what the library's own bounds check compares against — so every
        day between them is answerable, and the day after the last one
        genuinely is not.
        """
        cal = self.calendar
        return cal.first_session.date(), cal.last_session.date()

    def _is_session(self, d: date) -> bool:
        """Whether the exchange held a session on ``d``.

        Raises :class:`CalendarDateOutOfRange` for a date outside the
        supported window.
        """
        try:
            return bool(self.calendar.is_session(d))
        except DateOutOfBounds as e:
            first, last = self.supported_range
            raise CalendarDateOutOfRange(
                f"date {d.isoformat()} is outside the supported "
                f"{self.name} calendar range "
                f"({first.isoformat()} .. {last.isoformat()})"
            ) from e

    def is_trading_day(self, d: date) -> bool:
        """Whether the exchange traded on ``d``."""
        return self._is_session(d)

    def is_early_close(self, d: date) -> bool:
        """Whether ``d`` was a shortened session.

        A holiday is never an early close, and a date outside the
        supported window raises rather than returning ``False``.
        """
        if not self._is_session(d):
            return False
        return d in self._early_close_dates

    def session_times(self, d: date) -> tuple[datetime, datetime] | None:
        """The open and close instants of ``d``, or ``None`` if closed.

        The close time is the *special* close on a shortened session, so
        an early close reports 13:00 ET rather than 16:00 ET.
        """
        if not self._is_session(d):
            return None
        cal = self.calendar
        tz = getattr(cal, "tz", None)
        opens = cal.session_open(d).to_pydatetime()
        closes = cal.session_close(d).to_pydatetime()
        if tz is not None:
            opens = opens.astimezone(tz)
            closes = closes.astimezone(tz)
        return opens, closes

    def describe(self, d: date) -> dict[str, Any]:
        """Describe the market session for a single calendar date.

        Returns a plain dict; the wire shape is owned by
        :func:`app.api.schemas.build_market_calendar_response`.
        """
        if self._is_session(d):
            early = self.is_early_close(d)
            times = self.session_times(d) or (None, None)
            opens, closes = times
            return {
                "date": d,
                "status": STATUS_EARLY_CLOSE if early else STATUS_REGULAR,
                "is_trading_day": True,
                "is_holiday": False,
                "is_early_close": early,
                "open": opens,
                "close": closes,
            }

        weekend = d.weekday() >= 5
        return {
            "date": d,
            "status": STATUS_WEEKEND if weekend else STATUS_HOLIDAY,
            "is_trading_day": False,
            "is_holiday": not weekend,
            "is_early_close": False,
            "open": None,
            "close": None,
        }
