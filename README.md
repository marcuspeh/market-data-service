# market-data-service

A small FastAPI service that:

- **Proxies ETF constituents** (SPY, QQQ, IWM) from multiple data providers
  (SSGA, iShares) behind a single, provider-agnostic REST endpoint.
- **Caches market data** (OHLCV bars) from Polygon.io on local disk
  (parquet) with a configurable cache horizon so repeated requests don't
  re-hit the upstream API.
- **Serves today's bar live** from Interactive Brokers (IB Gateway /
  TWS) via the bundled `ib-gateway-docker` image — the bar is never
  persisted because it may still be forming.

The whole stack — FastAPI app **and** IB Gateway — runs in a single
Docker Compose service.

## Project layout

```
.
├── app/
│   ├── api/                       # FastAPI routers
│   │   ├── constituents.py
│   │   └── market_data.py
│   ├── clients/                   # External API clients
│   │   ├── ibkr.py                # IBKR TWS API wrapper (async + 5-min TTL)
│   │   └── polygon.py
│   ├── config/
│   │   └── settings.py            # Settings + NY timezone helpers
│   ├── services/
│   │   ├── constituents_fetcher.py    # Provider dispatch + registry
│   │   ├── constituents_service.py    # Cache + fetch orchestration
│   │   ├── constituents_scheduler.py  # APScheduler trigger
│   │   ├── market_data_service.py     # Bar cache + IBKR live bar
│   │   └── parsers/                   # Per-provider parsers
│   │       ├── ssga.py
│   │       └── ishares.py
│   └── main.py                    # FastAPI app + lifespan
├── Dockerfile                     # Single-service image (IB Gateway + app)
├── docker-compose.yml             # One service: market-data
├── docker-entrypoint.sh           # Boots IBC + waits for port 4004 + uvicorn
├── .dockerignore
├── pyproject.toml                 # uv-managed dependencies
└── uv.lock
```

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (manages the venv + dependencies)
- (For Polygon-backed endpoints) a Polygon.io API key
- An Interactive Brokers account for the live bar endpoint

## Setup

```bash
# 1. Install dependencies into .venv
uv sync

# 2. Copy and edit the env file
cp .env.example .env
# then fill in POLYGON_API_KEY and IB_* credentials
```

## Run

### Option A — Local Python with uv

Run the FastAPI app against an external IB Gateway (start IBC yourself
first).

```bash
uv run python -m app.main
# or
uv run uvicorn app.main:app --host 0.0.0.0 --port 8001
```

The app expects `IBKR_HOST=127.0.0.1` and the gateway listening on
port `4004` (paper) or `4003` (live). To override at runtime, set
`IBKR_HOST` and the gateway credentials accordingly.

### Option B — Docker Compose (recommended)

Brings up IB Gateway **and** the FastAPI app in a single container.
The image extends [`ghcr.io/gnzsnz/ib-gateway:latest`](https://github.com/gnzsnz/ib-gateway-docker)
and adds the Python app on top; `docker-entrypoint.sh` boots IBC in
the background, waits for the gateway to accept TWS API connections on
`127.0.0.1:4004`, then `exec`s uvicorn so the app inherits PID 1.

```bash
# 1. Fill in the IBKR + Polygon credentials
cp .env.example .env
$EDITOR .env   # set IB_USER, IB_PASSWORD, POLYGON_API_KEY

# 2. Launch everything
docker compose up -d --build

# 3. First-time IBKR setup
#    VNC into the gateway at localhost:5900 (no password by default;
#    set VNC_SERVER_PASSWORD to change) and:
#      - complete 2FA
#      - in TWS, accept the incoming API connection for clientId=1
#    Once accepted, the gateway auto-logs in on subsequent boots.

# 4. Tail logs
docker compose logs -f market-data
```

The app is exposed on `localhost:3556` (host port `APP_PORT`, default
`3556`); the gateway's VNC on `localhost:5900`. The app talks to the
gateway via `127.0.0.1:4004` (paper) — paper trading is the only mode
this deployment supports.

Paper trading only is enforced in `docker-compose.yml` and
`Settings.ibkr_trading_mode`. Live mode is intentionally not wired.

## Configuration

All settings come from environment variables (or a `.env` file via
`pydantic-settings`):

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATA_DIR` | `./data` | Root for parquet cache (`market/`, `constituents/`) |
| `POLYGON_API_KEY` | _empty_ | Polygon.io API key |
| `POLYGON_BASE_URL` | `https://api.polygon.io` | Override Polygon base URL (commented in `.env.example`) |
| `IB_USER` | _empty_ | IB Gateway TWS user id |
| `IB_PASSWORD` | _empty_ | IB Gateway TWS password |
| `IBKR_TIMEOUT_SECONDS` | `10` | Request timeout for the IBKR client |
| `APP_PORT` | `3556` | Host port the FastAPI app listens on |

Hardcoded inside `docker-compose.yml` / `Settings` (not env-overridable):

| Knob | Value | Why |
| --- | --- | --- |
| `IBKR_HOST` | `127.0.0.1` | App is in the same container as IB Gateway |
| `IBKR_TRADING_MODE` | `paper` | Paper trading only |
| `IBKR_CLIENT_ID` | `1` | Matches TWS "Trusted IPs" |
| `TRADING_MODE` (env in compose) | `paper` | Forwarded to IBC |

All date / time-of-day logic is anchored on **America/New_York**
(`zoneinfo.ZoneInfo("America/New_York")`). Bar dates, the IBKR cache
key, and the "today" check for backfill filtering all reason in ET.
See [`app/config/settings.py`](app/config/settings.py) for the
`ny_now`, `ny_from_ts`, and `ny_midnight_ts` helpers.

## API

The full OpenAPI doc is generated by FastAPI and is available at
`/docs` and `/redoc` once the server is running.

### `GET /health`

Liveness probe.

```bash
curl http://localhost:3556/health
# {"status":"ok"}
```

### `GET /constituents?etf={ETF}&date={YYYY-MM-DD}`

Returns the holding tickers of a supported ETF for a specific snapshot
date. Snapshots are stored on disk as one parquet file per ticker under
`constituents_dir` (default `./data/constituents/`) and refreshed by
`APScheduler` at 8:30 AM US/Eastern every day. If no snapshot exists
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
curl 'http://localhost:3556/constituents?etf=SPY&date=2026-08-12'
```

```json
{
  "symbol": "SPY",
  "date": "2026-08-12",
  "source": "parquet",
  "constituents": ["NVDA", "AAPL", "MSFT", "..."]
}
```

### `POST /admin/constituents/refresh`

Ad-hoc refresh. By default refreshes every supported ETF for today's
date (US Eastern). Optional `etf` query param restricts to a single
ticker; optional `date` overrides the snapshot date.

```bash
curl -X POST 'http://localhost:3556/admin/constituents/refresh?etf=SPY'
# {"date": "2026-08-12", "SPY": 505}
```

```bash
curl -X POST 'http://localhost:3556/admin/constituents/refresh'
# {"date": "2026-08-12", "results": {"SPY": 505, "QQQ": 106, "IWM": 1969}}
```

### `GET /market-data/{ticker}?from={YYYY-MM-DD}&to={YYYY-MM-DD}`

Returns **daily** OHLCV bars for a ticker. Historical bars
(`from..today-1`) are served from the local disk cache (parquet),
backfilled from Polygon.io on miss. **Today's bar** is fetched live
from Interactive Brokers via `IBKRClient` and is **never persisted** —
it may still be forming.

| Query param | Type | Default | Notes |
| --- | --- | --- | --- |
| `from` | `YYYY-MM-DD` | required | Start date (inclusive) |
| `to` | `YYYY-MM-DD` | today (NY) | End date (inclusive) |

Each bar in the response is tagged with `source`:

- `"cache"` — historical bar from the local parquet cache.
- `"ibkr"` — today's bar from Interactive Brokers.

**Quick example — AAPL over the last ~10 trading days:**

```bash
curl 'http://localhost:3556/market-data/AAPL?from=2026-08-10'
```

```json
{
  "ticker": "AAPL",
  "from": "2026-08-10",
  "to": "2026-08-20",
  "backfilled_bars": 0,
  "bars": [
    {"date": "2026-08-10", "ticker": "AAPL",
     "timestamp": 1786334400000,
     "open": 306.83, "high": 308.26, "low": 304.61, "close": 308.26,
     "volume": 44812503.00883, "vwap": 307.2588, "trade_count": 925003,
     "source": "cache"},
    {"date": "2026-08-11", "ticker": "AAPL",
     "timestamp": 1786420800000,
     "open": 307.75, "high": 309.97, "low": 302.79, "close": 304.91,
     "volume": 37476746.316505, "vwap": 305.6606, "trade_count": 762596,
     "source": "cache"},
    "..."
    {"ticker": "AAPL", "date": "2026-08-20",
     "timestamp": 1787198400000,
     "open": 317.46, "high": 320.28, "low": 315.96, "close": 317.54,
     "volume": 266731.0, "vwap": 317.295, "trade_count": 91062,
     "source": "ibkr"}
  ]
}
```

**Long-range example — full July through the live session:**

```bash
curl 'http://localhost:3556/market-data/AAPL?from=2026-07-01&to=2026-08-01'
```

**Notes:**

- A 502 response with body `IBKR upstream error: ...` means the
  IBKR TWS API call failed (e.g. no live market-data subscription,
  gateway offline). Historical bars from the cache are unaffected;
  you'll get them but the response for today's bar will be missing.
- `backfilled_bars` reports how many new bars were persisted to the
  cache during this request. Subsequent requests for the same range
  will not trigger backfill.
- "Today" is the **America/New_York** calendar date. At 00:00 UTC the
  US market is only 8 PM old, so the service still treats the prior
  US session as "today".

## Adding a new ETF

1. If the provider is new, add a parser under
   `app/services/parsers/<provider>.py` exposing
   `parse(content: bytes) -> list[dict]`. Each parser must return
   `{"ticker", "name", "weight}` dicts.
2. Register the parser in `PROVIDER_PARSERS` in
   [`app/services/constituents_fetcher.py`](app/services/constituents_fetcher.py).
3. Add a registry entry to `ETF_REGISTRY` with `provider` and `link`.

No other code changes are needed — the service auto-derives its
`SUPPORTED_SYMBOLS` set from the registry.

## License

MIT (or whatever your repo says — update as appropriate).