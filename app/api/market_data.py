import logging
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Path, Query

from app.clients.polygon import PolygonError
from app.config.settings import get_settings
from app.services.market_data_service import (
    MarketDataService,
    WindowTooLargeError,
)

router = APIRouter()
logger = logging.getLogger(__name__)

_settings = get_settings()
_service = MarketDataService(_settings)


@router.get("/market-data/{ticker}")
async def get_market_data(
    ticker: str = Path(..., description="Stock / ETF ticker symbol, e.g. AAPL"),
    from_: date = Query(
        ..., alias="from", description="Start date inclusive (YYYY-MM-DD)"
    ),
    to: date | None = Query(
        None, description="End date inclusive (YYYY-MM-DD). Defaults to today."
    ),
    timespan: str = Query("day", description="One of: day, hour, minute"),
    multiplier: int = Query(1, ge=1, le=100, description="Bar size multiplier"),
):
    if to is None:
        to = date.today()

    try:
        return await _service.get_bars(
            ticker=ticker,
            start=from_,
            end=to,
            timespan=timespan,
            multiplier=multiplier,
        )
    except WindowTooLargeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except PolygonError as e:
        logger.error(f"Polygon error for {ticker}: {e}")
        raise HTTPException(status_code=502, detail=f"Upstream error: {e}")
    except Exception as e:  # noqa: BLE001 — defensive top-level
        logger.error(f"Unexpected error retrieving market data for {ticker}: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to retrieve market data: {e}"
        )