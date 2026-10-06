from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.constituents import router as constituents_router
from app.api.market_data import router as market_data_router
from app.logging_setup import client, setup_logging, shutdown_logging
from app.middleware.access_log import AccessLogMiddleware
from app.middleware.correlation import CorrelationIdMiddleware
from app.services.constituents_scheduler import ConstituentsScheduler

setup_logging()
log = client()


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler: ConstituentsScheduler | None = None
    try:
        scheduler = ConstituentsScheduler()
        scheduler.start()
        log.info("Constituents scheduler started")
    except Exception as e:  # noqa: BLE001 — defensive top-level
        log.error("Failed to start constituents scheduler: %s", e)

    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.stop()
        shutdown_logging()


app = FastAPI(title="Market Data Service", lifespan=lifespan)
# Correlation id is added last so it wraps the access-log middleware and
# the generated id is already bound when the request/response logs fire.
app.add_middleware(AccessLogMiddleware)
app.add_middleware(CorrelationIdMiddleware)
app.include_router(constituents_router)
app.include_router(market_data_router)


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8001)