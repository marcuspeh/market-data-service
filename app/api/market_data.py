import logging
from datetime import date

from fastapi import APIRouter, HTTPException, Path, Query

from app.clients.longbridge import LongbridgeError
from app.clients.polygon import PolygonError
from app.config.settings import get_settings
from app.services.market_data_service import MarketDataService

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
):
    if to is None:
        to = date.today()

    try:
        return await _service.get_bars(
            ticker=ticker,
            start=from_,
            end=to,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except PolygonError as e:
        logger.error(f"Polygon error for {ticker}: {e}")
        raise HTTPException(status_code=502, detail=f"Polygon upstream error: {e}")
    except LongbridgeError as e:
        logger.error(f"Longbridge error for {ticker}: {e}")
        raise HTTPException(status_code=502, detail=f"Longbridge upstream error: {e}")
    except Exception as e:  # noqa: BLE001 — defensive top-level
        logger.error(f"Unexpected error retrieving market data for {ticker}: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to retrieve market data: {e}"
        )