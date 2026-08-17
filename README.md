# market-data-service

A small FastAPI service that:

- **Proxies ETF constituents** (SPY, QQQ, IWM) from multiple data providers
  (SSGA, iShares) behind a single, provider-agnostic REST endpoint.
- **Caches market data** (OHLCV bars) from Polygon.io on local disk
  (parquet) with a configurable cache horizon so repeated requests don't
  re-hit the upstream API.

## Project layout

```
.
├── app/
│   ├── api/              # FastAPI routers
│   │   ├── constituents.py
│   │   └── market_data.py
│   ├── clients/          # External API clients
│   │   └── polygon.py
│   ├── config/
│   │   └── settings.py   # Settings (loaded from .env via pydantic-settings)
│   ├── services/
│   │   ├── constituents_fetcher.py   # Provider dispatch + registry
│   │   ├── constituents_service.py   # Cache + fetch orchestration
│   │   ├── market_data_service.py    # Bar cache + backfill
│   │   └── parsers/                  # Per-provider parsers
│   │       ├── ssga.py
│   │       └── ishares.py
│   └── main.py           # FastAPI app + lifespan
├── Dockerfile            # Container image (multi-stage, uv-based)
├── docker-compose.yml    # app + ib-gateway-docker
├── .dockerignore
├── pyproject.toml        # uv-managed dependencies
└── uv.lock
```

## Requirements

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) (manages the venv + dependencies)
- (For Polygon-backed endpoints) a Polygon.io API key

## Setup

```bash
# 1. Install dependencies into .venv
uv sync

# 2. Copy and edit the env file
cp .env.example .env
# then fill in POLYGON_API_KEY and IBKR_* as needed
```

## Run

### Option A — Local Python with uv

```bash
uv run python -m app.main
# or
uv run uvicorn app.main:app --host 0.0.0.0 --port 8001
```

### Option B — Docker Compose (recommended)

Brings up the app and
[gnzsnz/ib-gateway-docker](https://github.com/gnzsnz/ib-gateway-docker).

```bash
# 1. Fill in the IBKR + Polygon credentials
cp .env.example .env
$EDITOR .env   # set IBKR_TWS_USERID, IBKR_TWS_PASSWORD, POLYGON_API_KEY, ...

# 2. Launch everything (app + ib-gateway)
docker compose up -d

# 3. First-time IBKR setup
#    VNC into the gateway at localhost:5900 (no password by default;
#    set VNC_SERVER_PASSWORD to change) and:
#      - complete 2FA
#      - in TWS, accept the incoming API connection for clientId=1
#    Then set IBKR_ACCEPT_INCOMING=auto in .env and restart ib-gateway.

# 4. Tail logs
docker compose logs -f app
```

The app is exposed on `localhost:8001`; the gateway's VNC on
`localhost:5900`. The app reaches the gateway via
`ib-gateway:4004` (paper) or `ib-gateway:4003` (live), depending on
`IBKR_TRADING_MODE`.

## Configuration

All settings come from environment variables (or a `.env` file via
`pydantic-settings`):

| Variable | Default | Purpose |
| --- | --- | --- |
| `POLYGON_API_KEY` | _empty_ | Polygon.io API key |
| `POLYGON_BASE_URL` | `https://api.polygon.io` | Override Polygon base URL |
| `IBKR_HOST` | `ib-gateway` | Hostname of the IB Gateway / TWS API |
| `IBKR_TRADING_MODE` | `paper` | `paper` or `live` |
| `IBKR_CLIENT_ID` | `1` | Unique per process; matches TWS "Trusted IPs" |
| `IBKR_TIMEOUT_SECONDS` | `10` | Request timeout for the IBKR client |
| `APP_PORT` | `8001` | Host port the FastAPI app listens on |

## API

### `GET /health`

Liveness probe.

```bash
curl http://localhost:8001/health
# {"status":"ok"}
```

### `GET /constituents?etf={ETF}&date={YYYY-MM-DD}`

Returns the holding tickers of a supported ETF for a specific snapshot
date. Snapshots are stored on disk as one parquet file per ticker under
`constituents_dir` (default `./data/constituents/`) and refreshed by
`APScheduler` ~1 hour before each US trading day. If no snapshot exists
for the requested date, returns **404**.

**Parquet schema** (one row per holding per snapshot date):

| Column | Type | Notes |
| --- | --- | --- |
| `date` | `date32` | Snapshot date |
| `ticker` | `string` | The constituent's ticker |

The ETF ticker itself is not stored as a column — it's the filename
(`SPY.parquet`, `QQQ.parquet`, `IWM.parquet`).

| Symbol | Provider | Source |
| --- | --- | --- |
| `SPY` | SSGA | `holdings-daily-us-en-spy.xlsx` |
| `QQQ` | iShares | iShares Nasdaq-100 ETF holdings CSV |
| `IWM` | iShares | iShares Russell 2000 ETF holdings CSV |

```bash
curl 'http://localhost:8001/constituents?etf=SPY&date=2026-08-12'
```

```json
{
  "symbol": "SPY",
  "date": "2026-08-12",
  "source": "parquet",
  "constituents": ["NVDA", "AAPL", "MSFT", ...]
}
```

### `POST /admin/constituents/refresh`

Ad-hoc refresh. By default refreshes every supported ETF for today's date
(US Eastern). Optional `etf` query param restricts to a single ticker;
optional `date` overrides the snapshot date.

```bash
curl -X POST 'http://localhost:8001/admin/constituents/refresh?etf=SPY'
# {"date": "2026-08-12", "SPY": 505}
```

```bash
curl -X POST 'http://localhost:8001/admin/constituents/refresh'
# {"date": "2026-08-12", "results": {"SPY": 505, "QQQ": 106, "IWM": 1969}}
```

### `GET /market-data/{ticker}`

Returns **daily** OHLCV bars for a ticker. Historical bars (`from..today-1`)
are served from the local disk cache (parquet), backfilled from
Polygon.io on miss. Today's bar is fetched live from Interactive Brokers
(via `IBKRClient`) and **never persisted** — it may still be forming.

| Query param | Type | Default | Notes |
| --- | --- | --- | --- |
| `from` | `YYYY-MM-DD` | required | Start date (inclusive) |
| `to` | `YYYY-MM-DD` | today | End date (inclusive) |

Any time range is supported. Each bar in the response is tagged with
`source` — `"cache"` for historical bars served from the local cache,
`"ibkr"` for today's live bar from Interactive Brokers.

```bash
curl 'http://localhost:8001/market-data/AAPL?from=2026-07-01&to=2026-08-01'
```

```json
{
  "ticker": "AAPL",
  "from": "2026-07-01",
  "to": "2026-08-01",
  "backfilled_bars": 22,
  "bars": [
    {"timestamp": 1752019200000,
     "open": 207.45, "high": 209.12, "low": 206.78, "close": 208.91,
     "volume": 48230000.0, "vwap": 208.02, "trade_count": 412567,
     "source": "cache"},
    {"timestamp": 1755043200000,
     "open": 215.30, "high": 216.10, "low": 214.80, "close": 215.95,
     "volume": 12340000.0, "vwap": 215.60, "trade_count": 98765,
     "source": "ibkr"}
  ]
}
```

## Adding a new ETF

1. If the provider is new, add a parser under
   `app/services/parsers/<provider>.py` exposing `parse(content: bytes) -> list[dict]`.
   Each parser must return `{"ticker", "name", "weight}` dicts.
2. Register the parser in `PROVIDER_PARSERS` in
   [`app/services/constituents_fetcher.py`](app/services/constituents_fetcher.py).
3. Add a registry entry to `ETF_REGISTRY` with `provider` and `link`.

No other code changes are needed — the service auto-derives its
`SUPPORTED_SYMBOLS` set from the registry.

## License

MIT (or whatever your repo says — update as appropriate).