# market-data-service Python SDK

Async-first client for the `market-data-service` proxy. Mirrors the two
routers exposed by the FastAPI app:

- `GET /constituents?etf={ETF}&date={YYYY-MM-DD}`
- `GET /market-data/{ticker}?from={YYYY-MM-DD}&to={YYYY-MM-DD}`

## Install

From this repo (no PyPI release yet):

```bash
# pip
pip install "market-data-service-sdk @ git+https://github.com/marcuspeh/market-data-service.git@main#subdirectory=sdk/python"

# uv
uv add "market-data-service-sdk @ git+https://github.com/marcuspeh/market-data-service.git@main#subdirectory=sdk/python"
```

Pin to a tag or commit SHA for reproducibility:

```bash
uv add "market-data-service-sdk @ git+https://github.com/marcuspeh/market-data-service.git@v0.1.0#subdirectory=sdk/python"
```

## Quick start

```python
import asyncio
from datetime import date
from market_data_service_sdk import (
    MarketDataClient,
    ConstituentsNotFoundError,
)

async def main():
    async with MarketDataClient(base_url="http://localhost:3556") as client:
        snapshot = await client.get_constituents("SPY", date(2026, 8, 12))
        print(snapshot.symbol, snapshot.constituents[:5])

        bars = await client.get_bars(
            "AAPL",
            from_=date(2026, 8, 10),
            to=date(2026, 8, 20),
        )
        for bar in bars.bars:
            print(bar.date, bar.close, bar.source)

asyncio.run(main())
```

A synchronous wrapper, `SyncMarketDataClient`, is also exported for
non-async callers.

## Errors

- `ConstituentsNotFoundError` — no snapshot for `(etf, date)` (HTTP 404).
- `MarketDataServiceError` — network failure, non-2xx response, malformed body.

## Run the tests

```bash
uv sync --extra test
uv run pytest
```
