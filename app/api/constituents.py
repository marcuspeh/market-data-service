import logging
from datetime import date

from fastapi import APIRouter, HTTPException, Query

from app.services.constituents_service import (
    ConstituentsService,
    SnapshotNotFoundError,
    UnsupportedSymbolError,
)

router = APIRouter()
logger = logging.getLogger(__name__)
_service = ConstituentsService()


@router.get("/constituents")
async def get_constituents(
    etf: str = Query(..., description="The ETF ticker symbol (e.g. SPY)"),
    date_: date = Query(
        ...,
        alias="date",
        description="The snapshot date (YYYY-MM-DD)",
    ),
):
    """Return the constituents of a supported ETF for the given snapshot date.

    Snapshots are populated by the APScheduler-driven daily refresh
    (see :mod:`app.services.constituents_scheduler`) — upstream providers
    only serve current-day data, so there's no ad-hoc refresh endpoint.
    Returns **404** if no snapshot exists for the requested date.
    """
    try:
        return _service.get_constituents(etf, date_)
    except UnsupportedSymbolError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except SnapshotNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Error retrieving constituents for {etf} on {date_}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retrieve constituents: {e}",
        )