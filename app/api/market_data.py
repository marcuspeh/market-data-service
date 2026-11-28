from datetime import date

from fastapi import APIRouter, HTTPException, Path, Query

from app.clients.longbridge import LongbridgeError
from app.clients.polygon import PolygonError
from app.config.settings import Settings, get_settings
from app.logging_setup import client
from app.services.market_data_service import MarketDataService

from app.api.schemas import (
    BarsResponseOut,
    BatchBarsRequestIn,
    BatchBarsResponseOut,
    SymbolErrorOut,
    build_bars_response,
)

router = APIRouter()
log = client()

_settings = get_settings()
_service = MarketDataService(_settings)


def _normalise_symbols(symbols: list[str]) -> list[str]:
    """Uppercase, trim and de-duplicate symbols, keeping request order."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in symbols:
        symbol = raw.strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        out.append(symbol)
    return out


def _error_status(exc: Exception) -> int:
    """HTTP status a per-symbol failure maps to."""
    if isinstance(exc, ValueError):
        return 400
    if isinstance(exc, (PolygonError, LongbridgeError)):
        return 502
    return 500


@router.get("/market-data/{ticker}", response_model=BarsResponseOut)
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
        # "Today" is the US/Eastern calendar date, matching every other
        # date decision the service makes.
        to = Settings.now_ny_date()

    try:
        result = await _service.get_bars(
            ticker=ticker,
            start=from_,
            end=to,
        )
        return build_bars_response(result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except PolygonError as e:
        log.error("Polygon error for %s: %s", ticker, e)
        raise HTTPException(status_code=502, detail=f"Polygon upstream error: {e}")
    except LongbridgeError as e:
        log.error("Longbridge error for %s: %s", ticker, e)
        raise HTTPException(status_code=502, detail=f"Longbridge upstream error: {e}")
    except Exception as e:  # noqa: BLE001 — defensive top-level
        log.error("Unexpected error retrieving market data for %s: %s", ticker, e)
        raise HTTPException(
            status_code=500, detail=f"Failed to retrieve market data: {e}"
        )


@router.post("/market-data/batch", response_model=BatchBarsResponseOut)
async def get_market_data_batch(payload: BatchBarsRequestIn):
    """Return bars for several symbols in one request.

    The symbols are fetched independently: a symbol that fails upstream
    is reported in ``errors`` instead of failing the whole batch.
    """
    from_ = payload.from_
    to = payload.to if payload.to is not None else Settings.now_ny_date()

    if to < from_:
        raise HTTPException(status_code=400, detail="'to' must be on or after 'from'")

    symbols = _normalise_symbols(payload.symbols)
    if not symbols:
        raise HTTPException(status_code=400, detail="No valid symbols supplied")

    log.info("Batch request for %d symbol(s)", len(symbols))
    outcome = await _service.get_bars_batch(symbols, from_, to)

    errors = []
    for item in outcome["errors"]:
        exc = item["error"]
        log.error("Batch fetch failed for %s: %s", item["ticker"], exc)
        errors.append(
            SymbolErrorOut(
                ticker=item["ticker"],
                status_code=_error_status(exc),
                detail=str(exc),
            )
        )

    return BatchBarsResponseOut(
        results=[build_bars_response(r) for r in outcome["results"]],
        errors=errors,
    )