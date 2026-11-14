from datetime import date

from fastapi import APIRouter, HTTPException, Query

from app.api.schemas import MarketCalendarOut, build_market_calendar_response
from app.logging_setup import client
from app.services.market_calendar import CalendarDateOutOfRange, MarketCalendar

router = APIRouter()
log = client()
_service = MarketCalendar()


@router.get("/market-calendar", response_model=MarketCalendarOut)
async def get_market_calendar(
    date_: date = Query(
        ..., alias="date", description="The calendar date (YYYY-MM-DD)"
    ),
):
    """Return the market session status for a single calendar date.

    Answers whether the exchange traded, whether the date was a market
    holiday, whether the session was shortened, and when it opened and
    closed. Dates are US/Eastern calendar days.

    Returns **400** for a date outside the supported calendar range.
    """
    try:
        return build_market_calendar_response(_service.describe(date_))
    except CalendarDateOutOfRange as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001 — defensive top-level
        log.error("Error retrieving market calendar for %s: %s", date_, e)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retrieve market calendar: {e}",
        )
