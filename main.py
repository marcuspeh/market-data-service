import logging
from fastapi import FastAPI, HTTPException, Query
from typing import List, Dict
from contextlib import asynccontextmanager

from database import init_db, get_cached_constituents, save_constituents
from parser import fetch_spy_constituents

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize database on startup
    try:
        init_db()
        logger.info("Database initialized")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
    yield

app = FastAPI(title="ETF Constituents Service", lifespan=lifespan)

@app.get("/constituents")
async def get_constituents(symbol: str = Query(..., description="The ETF symbol (e.g., SPY)")):
    symbol = symbol.upper()
    
    if symbol != "SPY":
        raise HTTPException(status_code=400, detail=f"Symbol '{symbol}' is not supported. Only SPY is supported currently.")
    
    try:
        # 1. Check cache (7 days TTL)
        cached_data = get_cached_constituents(symbol)
        if cached_data:
            logger.info(f"Returning cached constituents for {symbol}")
            return {"symbol": symbol, "constituents": cached_data, "source": "cache"}
        
        # 2. Fetch fresh data if cache is missing or expired
        logger.info(f"Cache miss/expired for {symbol}. Fetching fresh data...")
        fresh_data = fetch_spy_constituents()
        
        # 3. Update cache
        save_constituents(symbol, fresh_data)
        logger.info(f"Updated cache for {symbol}")
        
        return {"symbol": symbol, "constituents": fresh_data, "source": "external"}
        
    except Exception as e:
        logger.error(f"Error retrieving constituents for {symbol}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to retrieve constituents: {str(e)}")

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
