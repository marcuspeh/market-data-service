import logging

from fastapi import APIRouter, HTTPException, Query

from app.services.constituents_service import ConstituentsService, UnsupportedSymbolError

router = APIRouter()
logger = logging.getLogger(__name__)
_service = ConstituentsService()


@router.get("/constituents")
async def get_constituents(symbol: str = Query(..., description="The ETF symbol (e.g., SPY)")):
    try:
        return await _service.get_constituents(symbol)
    except UnsupportedSymbolError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error retrieving constituents for {symbol}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retrieve constituents: {e}",
        )