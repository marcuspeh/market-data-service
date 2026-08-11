import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.constituents import router as constituents_router
from app.api.market_data import router as market_data_router
from app.database.session import close_db, init_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await init_db()
        logger.info("Database initialized")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
    yield
    await close_db()


app = FastAPI(title="Market Data Service", lifespan=lifespan)
app.include_router(constituents_router)
app.include_router(market_data_router)


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)